#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import torch
import torch.nn as nn
import numpy as np
import hyperparams as hp

from utils import EOS
import time


class NoamOpt:
    """Optim wrapper that implements rate."""

    def __init__(self, model_size, factor, warmup, optimizer):
        self.optimizer = optimizer
        self._step = 0
        self.warmup = warmup
        self.factor = factor
        self.model_size = model_size
        self._rate = 0

    def step(self, loss=None):
        """Update parameters and rate"""
        self._step += 1
        rate = self.rate()
        for p in self.optimizer.param_groups:
            p['lr'] = rate
            # print("{}".format(rate))
        self._rate = rate
        self.optimizer.step()

    def rate(self, step=None):
        """Implement `lrate` above"""
        if step is None:
            step = self._step
        return self.factor * \
            (self.model_size ** (-0.5) *
             min(step ** (-0.5), step * self.warmup ** (-1.5)))


class CustomAdam:
    def __init__(self, optimizer, scheduler=None):
        self.optimizer = optimizer
        self.scheduler = scheduler
        self._step = 0
        self._rate = 0

    def step(self, loss=None):
        self._step += 1
        for p in self.optimizer.param_groups:
            self._rate = p['lr']
            # print("\n\n{}\n".format(self._rate))
        self.optimizer.step()
        if self.scheduler:
            self.scheduler.step(loss)


def get_std_opt(model):
    return NoamOpt(model.src_embed[0].d_model, 2, 4000,
                   torch.optim.Adam(model.parameters(), lr=0, betas=(0.9, 0.98), eps=1e-9))


class LabelSmoothing(nn.Module):
    """Implement label smoothing."""

    def __init__(self, size, padding_idx, smoothing=0.0):
        super(LabelSmoothing, self).__init__()
        self.criterion = nn.KLDivLoss(reduction='sum')
        self.padding_idx = padding_idx
        self.confidence = 1.0 - smoothing
        self.smoothing = smoothing
        self.size = size
        self.true_dist = None

    def forward(self, x, target):
        assert x.size(1) == self.size
        true_dist = x.data.clone()
        true_dist.fill_(self.smoothing / (self.size - 2))
        true_dist.scatter_(1, target.data.unsqueeze(1), self.confidence)
        true_dist[:, self.padding_idx] = 0
        mask = torch.nonzero(target.data == self.padding_idx, as_tuple=False)
        if mask.dim() > 0:
            true_dist.index_fill_(0, mask.squeeze(), 0.0)
        self.true_dist = true_dist
        return self.criterion(x, true_dist)


class Batch:
    """Object for holding a batch of data with mask during training."""

    def __init__(self, src, trg_set=None, pad=hp.pad_token):
        self.src = src
        self.src_mask = (src != pad).unsqueeze(-2)
        if trg_set is not None:
            trg = trg_set['trg']
            self.trg = trg[:, :-1, :]
            self.trg_y = trg[:, 1:, :]
            self.trg_mask = self.make_std_mask(self.trg, pad)
            self.nframes = (self.trg_y.sum(dim=-1) != pad).data.sum()
            self.trg_stops = trg_set['trg_stops']
            # print("trg_stops.shape:", self.trg_stops.shape)
            self.stop_tokens = self.trg_stops[:, 1:, :]
            # print("stop_tokens.shape:", self.stop_tokens.shape)

    @staticmethod
    def make_std_mask(tgt, pad):
        """Create a mask to hide padding and future words."""
        tgt_mask = (tgt.sum(dim=-1) != pad).unsqueeze(-2)
        tgt_mask = tgt_mask & subsequent_mask(
            tgt.size(-2)).type_as(tgt_mask.data)
        return tgt_mask


def subsequent_mask(size):
    """Mask out subsequent positions."""
    attn_shape = (1, size, size)
    mask = np.triu(np.ones(attn_shape), k=1).astype('uint8')
    return torch.from_numpy(mask) == 0


def data_gen(V, batch, nbatches, device):
    """Generate random data for a src-tgt copy task."""
    for i in range(nbatches):
        data = torch.from_numpy(np.random.randint(1, V, size=(batch, 10)))
        data[:, 0] = 1
        src = data.to(device)
        tgt = data.to(device)
        yield Batch(src, tgt, 0)


def data_prepare_tt2(batch_size, nbatches, random=False, sequential=False):
    """Prepare data for a src-tgt copy task of tt2 given src and tgt batch."""
    from utils import get_sample_batch
    src, tgt, tgt_stops, vocab = [], [], [], None

    def _append(b):
        src.append(b['src'])
        tgt.append(b['tgt'])
        tgt_stops.append(b['tgt_stops'])

    # different(sequential) data in each batch
    if sequential:
        for i in range(nbatches):
            batch = get_sample_batch(batch_size, start=i * batch_size,
                                     vocab=vocab, random=random)
            _append(batch)
            vocab = batch['vocab']

    # same data in each batch
    else:
        batch = get_sample_batch(batch_size, vocab=vocab, random=random)
        _append(batch)
        vocab = batch['vocab']

        for _ in range(nbatches-1):
            if random:
                batch = get_sample_batch(
                    batch_size, vocab=vocab, random=random)
                vocab = batch['vocab']
            _append(batch)

    return {'src': src, 'tgt': tgt, 'tgt_stops': tgt_stops, 'vocab': vocab}


def data_gen_tt2(data, device):
    """Generate data for a src-tgt copy task of tt2 given src and tgt batch."""
    for phoneme_batch, mel_batch, mel_stops in zip(data['src'], data['tgt'], data['tgt_stops']):
        yield Batch(phoneme_batch.to(device), {'trg': mel_batch.to(device), 'trg_stops': mel_stops.to(device)})


class SimpleLossCompute:
    """A simple loss compute and train function."""

    def __init__(self, generator, criterion, opt=None):
        self.generator = generator
        self.criterion = criterion
        self.opt = opt

    def __call__(self, x, y, norm):
        x = self.generator(x)
        loss = self.criterion(x.contiguous().view(-1, x.size(-1)),
                              y.contiguous().view(-1)) / norm
        loss.backward()
        if self.opt is not None:
            self.opt.step()
            self.opt.optimizer.zero_grad()
        return loss.data.item() * norm


class SimpleTT2LossCompute:
    """A simple loss compute and train function for tt2."""

    def __init__(self, criterion, stop_criterion, opt=None):
        self.criterion = criterion
        self.stop = stop_criterion
        self.opt = opt

    def __call__(self, x, y, stop_x, stop_y, norm, model):
        # calculate stop loss including impose positive weight as Sec 3.7.
        # print("stop_x shape and dtype", stop_x.shape, stop_x.dtype, stop_x[:,-3:,:])
        # print("stop_y shape and dtype", stop_y.shape, stop_y.dtype, stop_y[:,-3:,:])
        stop_loss = self.stop(stop_x, stop_y)
        stop_loss[:, -1, :] *= hp.positive_stop_weight
        stop_loss = torch.mean(stop_loss)

        loss = self.criterion(x, y) + hp.loss_w_stop * stop_loss
        t0 = time.time()
        loss.backward()
        print('backprop: %.6f' % (time.time() - t0))
        nn.utils.clip_grad_norm_(model.parameters(), 1.)
        if self.opt is not None:
            self.opt.step(loss)
            self.opt.optimizer.zero_grad()
            # self.opt.zero_grad()
        return loss.data.item() * norm


def rebatch(pad_idx, batch):
    """Fix order in torchtext to match ours"""
    src, trg = batch.src.transpose(0, 1), batch.trg.transpose(0, 1)
    return Batch(src, trg, pad_idx)
