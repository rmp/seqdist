# AUTOGENERATED! DO NOT EDIT! File to edit: notebooks/05_Sparse.ipynb (unless otherwise specified).

__all__ = ['device', 'Mv_scan_py', 'transpose', 'logZ_py', 'ctc_loss_py', 'Mv_scan_cupy', 'cupy_funcs', 'logZ',
           'ctc_loss']

# Cell
from functools import partial
import numpy as np
import cupy as cp
import torch
from .utils import *
from .ctc import semiring, Max, Log, interleave_blanks, generate_sample_inputs, loss_pytorch, benchmark_fwd_bwd, report, compare_fwd_bwd

device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')

# Cell
def Mv_scan_py(Ms, idx, v0, S:semiring=Log):
    T, N, C, nz = Ms.shape
    alpha = Ms.new_full((T+1, N, C), S.zero)
    alpha[0] = v0
    for t in range(T):
        alpha[t+1] = S.sum(S.mul(Ms[t], alpha[t, :, idx]), dim=2)
    return alpha

def transpose(Ms, idx):
    T, N, C, nz = Ms.shape
    assert idx.shape == (C, nz)
    i = idx.flatten().argsort().reshape(C, nz)
    idx_T = i // nz
    Ms_T = Ms.reshape(T, N, -1)[:, :, i]
    return Ms_T, idx_T

class _LogZ(torch.autograd.Function):
    @staticmethod
    def forward(ctx, Ms, idx, v0, vT, S:semiring, scan):
        alpha = scan(Ms, idx, v0, S)
        ctx.save_for_backward(alpha, Ms, idx, vT)
        ctx.semiring, ctx.scan = S, scan
        return S.sum(S.mul(alpha[-1], vT), dim=1)

    @staticmethod
    def backward(ctx, grad):
        alpha, Ms, idx, vT = ctx.saved_tensors
        S, scan = ctx.semiring, ctx.scan
        T, N, C, nz = Ms.shape
        Ms_T, idx_T = transpose(Ms, idx)
        beta = scan(Ms_T.flip(0), idx_T, vT, S)
        g = S.mul(S.mul(Ms.reshape(T, N, -1), alpha[:-1, :, idx.flatten()]).reshape(T, N, C, nz), beta[:-1, :, :, None].flip(0))
        g = S.dsum(g.reshape(T, N, -1), dim=2).reshape(T, N, C, nz)
        return grad[None, :, None, None] * g, None, None, None, None, None

logZ_py = partial(_LogZ.apply, S=Log, scan=Mv_scan_py)

# Cell
from torch.nn.functional import pad

def _ctc_loss(logits, targets, input_lengths, target_lengths, scan, S:semiring=Log):
    zero, one = [logits.new_full((1,), x) for x in (S.zero, S.one)]
    scores = logits.log_softmax(2)
    states = interleave_blanks(targets, blank_idx=0)
    state_scores = torch.gather(scores, 2, states.expand(scores.size(0), -1, -1))
    final_states = torch.stack([target_lengths*2-1, target_lengths*2], 1)

    T, N, Lp = state_scores.shape
    assert torch.all(input_lengths == T)

    Ms = torch.stack([
        state_scores,
        pad(state_scores[:, :, 1:], (1, 0), value=S.zero),
        pad(torch.where(states[:, 2:] == states[:, :-2], zero.expand(T, N, Lp-2), state_scores[:, :, 2:]), (2, 0), value=S.zero)
    ], -1)

    i = torch.arange(Lp, device=device)
    rot = lambda x, n: torch.cat([x[-n:], x[:-n]])
    idx = torch.stack([i, rot(i, 1), rot(i, 2)], dim=1)

    v0 = torch.cat([one.expand(N, 1), zero.expand(N, Lp - 1)], dim=1)
    vT = zero.expand(N, Lp).clone().scatter_(1, final_states, S.one)

    logZ = _LogZ.apply(Ms, idx, v0, vT, S, scan)
    return -(logZ / target_lengths).mean()

ctc_loss_py = partial(_ctc_loss, scan=Mv_scan_py)

# Cell
cupy_funcs = {
    (torch.float32, Log): load_cupy_func('cuda/sparse.cu', 'sparse_Mv_scan', FLOAT='float',  ADD='logsumexp2', MUL='add', ZERO='{:E}'.format(Log.zero)),
    (torch.float64, Log): load_cupy_func('cuda/sparse.cu', 'sparse_Mv_scan', FLOAT='double',  ADD='logsumexp2', MUL='add', ZERO='{:E}'.format(Log.zero)),
    (torch.float32, Max): load_cupy_func('cuda/sparse.cu', 'sparse_Mv_scan', FLOAT='float',  ADD='max2', MUL='add', ZERO='{:E}'.format(Log.zero)),
    (torch.float64, Max): load_cupy_func('cuda/sparse.cu', 'sparse_Mv_scan', FLOAT='double',  ADD='max2', MUL='add', ZERO='{:E}'.format(Log.zero)),
}

def Mv_scan_cupy(Ms, idx, v0, S:semiring):
    T, N, C, nz = Ms.shape
    assert idx.shape == (C, nz)
    alpha = Ms.new_full((T+1, N, C), S.zero)
    alpha[0] = v0
    with cp.cuda.Device(Ms.device.index):
        cupy_funcs[(Ms.dtype, S)](grid=(N, 1, 1), block=(C, 1, 1), shared_mem=2*8*C,
               args=(alpha.data_ptr(), Ms.data_ptr(), idx.to(dtype=torch.int, device=Ms.device).data_ptr(), T, N, C, nz))
    return alpha

logZ = partial(_LogZ.apply, S=Log, scan=Mv_scan_cupy)

ctc_loss = partial(_ctc_loss, scan=Mv_scan_cupy)