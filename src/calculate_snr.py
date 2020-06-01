import pathlib
import pickle
import json
import os
import datetime
import numpy as np
from pprint import pprint

import torch

from src.helpers.torch_metrics import ssim
from src.helpers.data_loading import create_data_loaders
from src.impro_models.convpool_model import build_impro_convpool_model
from src.recon_models.recon_model_utils import (get_new_zf, create_impro_model_input, load_recon_model,
                                                acquire_new_zf_exp_batch, acquire_new_zf_batch)  # for greedy
from src.impro_models.impro_model_utils import impro_model_forward_pass, build_optim


def create_data_range_dict(args, loader):
    # Locate ground truths of a volume
    gt_vol_dict = {}
    for it, data in enumerate(loader):
        # TODO: Use fname, slice to create state-step-dependent baseline
        # TODO: use fname and initial loop over gt to find data range per fname
        kspace, masked_kspace, mask, zf, gt, gt_mean, gt_std, fname, slice = data
        for i, vol in enumerate(fname):
            if vol not in gt_vol_dict:
                gt_vol_dict[vol] = []
            gt_vol_dict[vol].append(gt[i] * gt_std[i] + gt_mean[i])

    # Find max of a volume
    data_range_dict = {}
    for vol, gts in gt_vol_dict.items():
        # Shape 1 x 1 x 1 x 1
        data_range_dict[vol] = torch.stack(gts).max().unsqueeze(-1).unsqueeze(-1).unsqueeze(-1).to(args.device)
    del gt_vol_dict

    return data_range_dict


def get_rewards_nongreedy(args, res, mask, masked_kspace, recon_model, gt_mean, gt_std, unnorm_gt, data_range, k):
    batch_mk = masked_kspace.view(mask.size(0) * k, 1, res, res, 2)
    # Get new zf: shape = (batch . num_rows x 1 x res x res)
    zf, _, _ = get_new_zf(batch_mk)
    # Get new reconstruction
    impro_input = create_impro_model_input(args, recon_model, zf, mask)
    # shape = batch . k x 1 x res x res, extract reconstruction to compute target
    recons = impro_input[:, 0:1, ...]
    # shape = batch x k x res x res
    recons = recons.view(mask.size(0), k, res, res)
    unnorm_recons = recons * gt_std + gt_mean
    gt_exp = unnorm_gt.expand(-1, k, -1, -1)
    # scores = batch x k (channels), base_score = batch x 1
    scores = ssim(unnorm_recons, gt_exp, size_average=False, data_range=data_range).mean(-1).mean(-1)
    return scores, impro_input


def get_rewards_greedy(args, kspace, masked_kspace, mask, unnorm_gt, gt_mean, gt_std, recon_model, impro_input,
                       actions, output, data_range):
    # actions is a batch x k tensor, containing row indices to compute targets for

    recon = impro_input[:, 0:1, ...]  # Other channels are uncertainty maps + other input to the impro model
    unnorm_recon = recon * gt_std + gt_mean  # Back to original scale for metric

    # shape = batch x 1
    base_score = ssim(unnorm_recon, unnorm_gt, size_average=False,
                      data_range=data_range).mean(-1).mean(-1)  # keep channel dim = 1

    res = mask.size(-2)
    # Acquire chosen rows, and compute the improvement target for each (batched)
    # shape = batch x rows = k x res x res
    zf_exp, _, _ = acquire_new_zf_exp_batch(kspace, masked_kspace, actions)
    # shape = batch . k x 1 x res x res, so that we can run the forward model for all rows in the batch
    zf_input = zf_exp.view(actions.size(0) * actions.size(1), 1, res, res)
    # shape = batch . k x 2 x res x res
    recons_output = recon_model(zf_input)
    # shape = batch . k x 1 x res x res, extract reconstruction to compute target
    recons = recons_output[:, 0:1, ...]
    # shape = batch x k x res x res
    recons = recons.view(actions.size(0), actions.size(1), res, res)
    unnorm_recons = recons * gt_std + gt_mean  # TODO: Normalisation necessary?
    gt_exp = unnorm_gt.expand(-1, actions.size(1), -1, -1)
    # scores = batch x k (channels), base_score = batch x 1
    scores = ssim(unnorm_recons, gt_exp, size_average=False, data_range=data_range).mean(-1).mean(-1)
    impros = (scores - base_score) * 1  # TODO: is this 'normalisation'?
    # target = batch x rows, batch_train_rows and impros = batch x k
    # target = torch.zeros(actions.size(0), res).to(args.device)
    target = output.detach().clone()
    for j, train_rows in enumerate(actions):
        # impros[j, 0] (slice j, row 0 in train_rows[j]) corresponds to the row train_rows[j, 0] = 9
        # (for instance). This means the improvement 9th row in the kspace ordering is element 0 in impros.
        kspace_row_inds, permuted_inds = train_rows.sort()
        target[j, kspace_row_inds] = impros[j, permuted_inds]
    return target


# Greedy
def acquire_row(args, kspace, masked_kspace, next_rows, mask, recon_model):
    zf, mean, std = acquire_new_zf_batch(kspace, masked_kspace, next_rows)
    # Don't forget to change mask for impro_model (necessary if impro model uses mask)
    # Also need to change masked kspace for recon model (getting correct next-step zf)
    # TODO: maybe do this in the acquire_new_zf_batch() function. Doesn't fit with other functions of same
    #  description, but this one is particularly used for this acquisition loop.
    for sl, next_row in enumerate(next_rows):
        mask[sl, :, :, next_row, :] = 1.
        masked_kspace[sl, :, :, next_row, :] = kspace[sl, :, :, next_row, :]
    # Get new reconstruction for batch
    impro_input = create_impro_model_input(args, recon_model, zf, mask)  # TODO: args is global here!
    return impro_input, zf, mean, std, mask, masked_kspace


# Nongreedy
def get_policy_probs(output, unacquired):
    # Reshape policy output such that we can use the same policy for different shapes of acquired
    # This should only be applied in the first acquisition step to initialise trajectories, since after that 'output'
    # should have the shape of batch x num_trajectories x res already.
    if len(output.shape) != len(unacquired.shape):
        output = output.view(output.size(0), 1, output.size(-1))
        output = output.repeat(1, unacquired.size(1), 1)
    # Mask acquired rows
    logits = torch.where(unacquired.byte(), output, -1e7 * torch.ones_like(output))
    # Softmax over 'logits' representing row scores
    probs = torch.nn.functional.softmax(logits - torch.max(logits, dim=-1, keepdim=True)[0], dim=-1)
    return probs


# Nongreedy
def acquire_rows_in_batch_parallel(k, mk, mask, to_acquire):
    # TODO: This is a version of acquire_new_zf_exp_batch returns mask instead of zf: integrate this nicely
    if mask.size(1) == mk.size(1) == to_acquire.size(1):
        # We are already in a trajectory: every row in to_acquire corresponds to an existing trajectory that
        # we have sampled the next row for.
        m_exp = mask
        mk_exp = mk
    else:
        # We have to initialise trajectories: every row in to_acquire corresponds to the start of a trajectory.
        m_exp = mask.repeat(1, to_acquire.size(1), 1, 1, 1)
        mk_exp = mk.repeat(1, to_acquire.size(1), 1, 1, 1)
    # Loop over slices in batch
    for sl, rows in enumerate(to_acquire):
        # Loop over indices to acquire
        for index, row in enumerate(rows):
            m_exp[sl, index, :, row.item(), :] = 1.
            mk_exp[sl, index, :, row.item(), :] = k[sl, 0, :, row.item(), :]
    return m_exp, mk_exp


def add_impro_args(args, impro_args):
    args.accelerations = impro_args.accelerations
    args.reciprocals_in_center = impro_args.reciprocals_in_center
    args.center_fractions = impro_args.center_fractions
    args.resolution = impro_args.resolution
    args.in_chans = impro_args.in_chans
    args.run_dir = impro_args.run_dir

    # For greedy model
    if 'estimator' in impro_args.__dict__:
        args.estimator = impro_args.estimator
    if 'acq_strat' in impro_args.__dict__:
        args.acq_strat = impro_args.acq_strat
    return args


def load_impro_model(checkpoint_file):
    checkpoint = torch.load(checkpoint_file)
    args = checkpoint['args']
    model = build_impro_convpool_model(args)

    # Only store gradients for final layer
    for name, param in model.named_parameters():
        if name in ["fc_out.4.weight", "fc_out.4.bias"]:
            param.requires_grad = True
        else:
            param.requires_grad = False

    if args.data_parallel:
        model = torch.nn.DataParallel(model)
    model.load_state_dict(checkpoint['model'])

    optimizer = build_optim(args, model.parameters())
    optimizer.load_state_dict(checkpoint['optimizer'])
    start_epoch = checkpoint['epoch']
    del checkpoint

    return model, args, start_epoch, optimizer


def snr_from_grads(grads, style):
    if style == 'det':
        snr = det_snr_from_grads(grads)
    elif style == 'stoch':
        snr = stoch_snr_from_grads(grads)
    else:
        raise ValueError()

    return snr


def stoch_snr_from_grads(grads):
    # 1 x last_layer_size x (second_to_last_layer_size + 1)
    mean = np.mean(grads, axis=0, keepdims=True)
    var = np.mean((grads - mean) ** 2, axis=0, keepdims=True)

    # variance of mean = variance of sample / num_samples (batches)
    var = var / grads.shape[0]

    # 1 x last_layer_size (x secon_to_last_layer_size)

    #     # 1) mean / stddev in every direction of the loss surface (all weights individually)
    #     snr = np.abs(mean) / np.sqrt(var)
    #     # scalar
    #     snr = snr.mean()

    # 2) mean / stddev in norm: magnitude of mean and stddev in the high-dim loss surface
    snr = np.linalg.norm(mean) / np.linalg.norm(np.sqrt(var))

    return snr


def det_snr_from_grads(grads):
    # num_batches x last_layer_size x (second_to_last_layer_size + 1)

    # 1 x last_layer_size x (second_to_last_layer_size + 1)
    true_grad = grads.mean(axis=0)
    true_grad_flat = true_grad.flatten()
    # 1 x (second_to_last_layer_size + 1)
    norm = np.linalg.norm(true_grad_flat)

    snr = 0
    for grad in grads:
        paral_norm = (np.dot(grad.flatten(), true_grad.flatten()) / norm)
        vec_dir = true_grad / norm
        paral_grad = paral_norm * vec_dir
        perpen_grad = grad - paral_grad

        #     print(paral_norm.shape, vec_dir.shape, paral_grad.shape, perpen_grad.shape)
        #     print(paral_norm)
        #     print(vec_dir)
        #     print(paral_grad)
        #     print(perpen_grad)

        signal = np.dot(paral_grad.flatten(), paral_grad.flatten())
        noise = np.dot(perpen_grad.flatten(), perpen_grad.flatten())

        #     print(signal, noise)
        #     print(signal / noise)
        snr += signal / noise

    snr /= len(grads)
    return snr


def compute_snr(weight_path, bias_path, style):
    with open(weight_path, 'rb') as f:
        weight_list = pickle.load(f)
    with open(bias_path, 'rb') as f:
        bias_list = pickle.load(f)

    # num_batches x last_layer_size x second_to_last_layer_size
    weight_grads = np.stack(weight_list)
    # num_batches x last_layer_size x 1 (after reschape)
    bias_grads = np.stack(bias_list)[:, :, None]

    # num_batches x last_layer_size x (second_to_last_layer_size + 1)
    grads = np.concatenate((weight_grads, bias_grads), axis=-1)

    snr = snr_from_grads(grads, style)
    return snr


def compute_gradients(args):
    param_dir = (f'mepoch{args.m_epoch}_t{args.num_trajectories}_sr{args.sample_rate}'
                 f'_runs{args.data_runs}_batch{args.batch_size}_bs{args.batches_step}')
    param_dir = args.impro_model_checkpoint.parent / param_dir
    param_dir.mkdir(parents=True, exist_ok=True)

    # Create storage path
    weight_path = param_dir / f'weight_grads_r{args.data_runs}_it{args.iters}.pkl'
    bias_path = param_dir / f'bias_grads_r{args.data_runs}_it{args.iters}.pkl'
    # Check if already computed (skip computing again if not args.force_computation)
    if weight_path.exists() and bias_path.exists() and not args.force_computation:
        print(f'Gradients already stored in: \n    {weight_path}\n    {bias_path}')
        return weight_path, bias_path, param_dir

    start_run = 0
    weight_grads = []
    bias_grads = []

    # Check if some part of the gradients already computed
    for r in range(args.data_runs, 0, -1):
        tmp_param_dir = (f'mepoch{args.m_epoch}_t{args.num_trajectories}_sr{args.sample_rate}'
                         f'_runs{r}_batch{args.batch_size}_bs{args.batches_step}')
        tmp_weight_path = args.impro_model_checkpoint.parent / tmp_param_dir / f'weight_grads_r{r}_it{args.iters}.pkl'
        tmp_bias_path = args.impro_model_checkpoint.parent / tmp_param_dir / f'bias_grads_r{r}_it{args.iters}.pkl'
        # If part already computed, skip this part of the computation by setting start_run to the highest
        # computed run. Also load the weights.
        if tmp_weight_path.exists() and tmp_bias_path.exists() and not args.force_computation:
            print(f'Gradients up to run {r} already stored in: \n    {tmp_weight_path}\n    {tmp_bias_path}')
            with open(tmp_weight_path, 'rb') as f:
                weight_grads = pickle.load(f)
            with open(tmp_bias_path, 'rb') as f:
                bias_grads = pickle.load(f)
            start_run = r
            break

    model, impro_args, start_epoch, optimiser = load_impro_model(args.impro_model_checkpoint)
    add_impro_args(args, impro_args)

    recon_args, recon_model = load_recon_model(args)

    train_loader, dev_loader, test_loader, _ = create_data_loaders(args)
    loader = train_loader
    data_range_dict = create_data_range_dict(args, loader)

    k = args.num_trajectories
    for r in range(start_run, args.data_runs):
        print(f"\n    Run {r + 1} ...")
        ssims = 0
        cbatch = 0
        tbs = 0
        for it, data in enumerate(loader):  # Randomly shuffled every time
            if args.iters is not None:
                if it == args.iters:
                    break
            kspace, masked_kspace, mask, zf, gt, gt_mean, gt_std, fname, sl_idx = data
            cbatch += 1
            # shape after unsqueeze = batch x channel x columns x rows x complex
            kspace = kspace.unsqueeze(1).to(args.device)
            masked_kspace = masked_kspace.unsqueeze(1).to(args.device)
            mask = mask.unsqueeze(1).to(args.device)
            # shape after unsqueeze = batch x channel x columns x rows
            zf = zf.unsqueeze(1).to(args.device)
            gt = gt.unsqueeze(1).to(args.device)
            gt_mean = gt_mean.unsqueeze(1).unsqueeze(2).unsqueeze(3).to(args.device)
            gt_std = gt_std.unsqueeze(1).unsqueeze(2).unsqueeze(3).to(args.device)
            unnorm_gt = gt * gt_std + gt_mean

            data_range = torch.stack([data_range_dict[vol] for vol in fname])

            tbs += mask.size(0)

            impro_input = create_impro_model_input(args, recon_model, zf, mask)
            unnorm_recon = impro_input[:, 0:1, :, :] * gt_std + gt_mean
            base_score = ssim(unnorm_recon, unnorm_gt, size_average=False,
                              data_range=data_range).mean(dim=(-1, -2))
            batch_ssims = [base_score.sum().item()]

            if cbatch == 1:
                optimiser.zero_grad()

            if args.mode == 'greedy':
                batch_ssims = greedy_trajectory(args, model, recon_model, kspace, mask, masked_kspace, gt_mean, gt_std,
                                                unnorm_gt,
                                                data_range, impro_input, k, batch_ssims)
            elif args.mode == 'nongreedy':
                batch_ssims = nongreedy_trajectory(args, model, recon_model, kspace, mask, masked_kspace, gt_mean,
                                                   gt_std, unnorm_gt,
                                                   data_range, impro_input, k, base_score, batch_ssims)
            else:
                raise ValueError()

            if cbatch == args.batches_step:
                # Store gradients for SNR
                for name, param in model.named_parameters():
                    if name == "module.fc_out.4.weight":
                        weight_grads.append(param.grad.cpu().numpy())
                    elif name == "module.fc_out.4.bias":
                        bias_grads.append(param.grad.cpu().numpy())
                cbatch = 0

            # shape = al_steps
            ssims += np.array(batch_ssims)

        ssims /= tbs
        print(f"     - ssims: \n       {ssims}")

        print(f"     - Saving grads of run {r + 1} to: \n       {param_dir}")
        with open(weight_path, 'wb') as f:
            pickle.dump(weight_grads, f)
        with open(bias_path, 'wb') as f:
            pickle.dump(bias_grads, f)

    return weight_path, bias_path, param_dir


def greedy_trajectory(args, model, recon_model, kspace, mask, masked_kspace, gt_mean, gt_std, unnorm_gt,
                      data_range, impro_input, k, batch_ssims):
    for step in range(args.acquisition_steps):
        output, _ = impro_model_forward_pass(args, model, impro_input, mask.squeeze(1).squeeze(1).squeeze(-1))
        loss_mask = (mask == 0).squeeze().float()

        # Mask acquired rows
        logits = torch.where(loss_mask.byte(), output, -1e7 * torch.ones_like(output))
        # logits = output

        # Softmax over 'logits' representing row scores
        probs = torch.nn.functional.softmax(logits - torch.max(logits, dim=1, keepdim=True)[0], dim=1)
        # # TODO: this possibly samples non-allowed rows sometimes, which have prob ~ 0, and thus log prob -inf.
        # #  To fix this we'd need to restrict the categorical to only allowed rows, keeping track of the indices,
        # #  so that we correctly backpropagate the loss to the model.

        # Also need this for sampling the next row at the end of this loop
        policy = torch.distributions.Categorical(probs)
        if args.estimator == 'wr':
            # batch x k
            actions = policy.sample((k,)).transpose(0, 1)  # TODO: DiCE estimator; differentiable sampling?
        #         elif args.estimator == 'wor':
        #             actions = torch.multinomial(probs, k, replacement=False)
        else:
            raise ValueError(f'{args.estimator} is not a valid estimator.')

        # REINFORCE-like with baselines
        target = get_rewards_greedy(args, kspace, masked_kspace, mask, unnorm_gt, gt_mean, gt_std, recon_model,
                                    impro_input, actions, output, data_range)

        # TODO: Only works if all actions are unique within a sample
        # batch x k
        action_logprobs = torch.log(torch.gather(probs, 1, actions))
        action_rewards = torch.gather(target, 1, actions)

        if args.estimator == 'wr':
            # With replacement
            # batch x 1
            avg_reward = torch.mean(action_rewards, dim=1, keepdim=True)
            # REINFORCE with self-baselines
            # batch x k
            loss = -1 * (action_logprobs * (action_rewards - avg_reward)) / (actions.size(1) - 1)
            # batch
            loss = loss.sum(dim=1)

        #         elif args.estimator == 'wor':
        #             # Without replacement
        #             # batch x 1
        #             loss = reinforce_unordered(-1 * action_rewards, action_logprobs)
        else:
            raise ValueError(f'{args.estimator} is not a valid estimator.')

        loss = loss.mean()
        loss.backward()

        if args.acq_strat == 'max':
            # Acquire row for next step: GREEDY
            _, next_rows = torch.max(logits, dim=1)  # TODO: is greedy a good idea? Acquire multiple maybe?
        elif args.acq_strat == 'sample':
            # Acquire next row by sampling
            next_rows = policy.sample()
        else:
            raise ValueError(f'{args.acq_strat} is not a valid acquisition strategy')

        impro_input, zf, _, _, mask, masked_kspace = acquire_row(args, kspace, masked_kspace, next_rows, mask,
                                                                 recon_model)

        unnorm_recon = impro_input[:, 0:1, :, :] * gt_std + gt_mean
        ssim_val = ssim(unnorm_recon, unnorm_gt, size_average=False,
                        data_range=data_range).mean(dim=(-1, -2)).sum()

        # Average over trajectories, sum over batch dimension
        batch_ssims.append(ssim_val.item())

    return batch_ssims


def nongreedy_trajectory(args, model, recon_model, kspace, mask, masked_kspace, gt_mean, gt_std, unnorm_gt,
                         data_range, impro_input, k, base_score, batch_ssims):
    action_list = []
    logprob_list = []
    reward_list = []

    # Initial policy
    impro_output, _ = impro_model_forward_pass(args, model, impro_input, mask.squeeze(1).squeeze(1).squeeze(-1))

    # Need to squeeze all dims but batch and row dim here
    unacquired = (mask == 0).squeeze(-4).squeeze(-3).squeeze(-1).float()
    probs = get_policy_probs(impro_output, unacquired)

    for step in range(args.acquisition_steps):
        # Sample initial actions from policy: batch x k
        if step == 0:
            actions = torch.multinomial(probs, k, replacement=True)
        else:  # Here policy has batch_shape = (batch x num_trajectories), so we just need a sample
            # Since we're only taking a single sample per trajectory, this is 'without replacement'
            policy = torch.distributions.Categorical(probs)
            actions = policy.sample()

        # Store actions and logprobs for later gradient estimation
        # action_tensor[:, :, step] = actions
        action_list.append(actions)

        if step == 0:
            # Parallel sampling of multiple actions
            # probs shape = (batch x res), actions shape = (batch, num_trajectories
            logprobs = torch.log(torch.gather(probs, 1, actions))
        else:
            # Single action per trajectory
            # probs shape = (batch x num_trajectories x res), actions shape = (batch, num_trajectories)
            # Reshape to (batch . num_trajectories x 1\res) for easy gathering
            # Then reshape result back to (batch, num_trajectories)
            selected_probs = torch.gather(
                probs.view(unnorm_gt.size(0) * k, args.resolution),
                1,
                actions.view(unnorm_gt.size(0) * k, 1)).view(actions.shape)
            logprobs = torch.log(selected_probs)

        logprob_list.append(logprobs)

        # Initial acquisition: add rows to mask in parallel (shape = batch x num_rows x 1\res x res x 2)
        # NOTE: In the first step, this changes mask shape to have size num_rows rather than 1 in dim 1.
        #  This results in unacquired also obtaining this shape. Hence, get_policy_probs requires that
        #  output is also this shape.
        mask, masked_kspace = acquire_rows_in_batch_parallel(kspace, masked_kspace, mask, actions)

        # Option 3)
        scores, impro_input = get_rewards_nongreedy(args, args.resolution, mask, masked_kspace, recon_model,
                                                    gt_mean, gt_std, unnorm_gt, data_range, k)

        # Store rewards shape = (batch x num_trajectories)
        # reward_tensor[:, :, step] = scores - base_score
        reward = scores - base_score
        reward_list.append(reward)

        # Set new base_score (we learn improvements)
        base_score = scores

        # If not final step: get policy for next step from current reconstruction
        if step != args.acquisition_steps - 1:
            # Get policy model output
            impro_output, _ = impro_model_forward_pass(args, model, impro_input,
                                                       mask.view(unnorm_gt.size(0) * k, args.resolution))
            # Shape back to batch x num_trajectories x res
            impro_output = impro_output.view(unnorm_gt.size(0), k, args.resolution)
            # Mutate unacquired so that we can obtain a new policy on remaining rows
            # Need to make sure the channel dim remains unsqueezed when k = 1
            unacquired = (mask == 0).squeeze(-3).squeeze(-1).float()
            # Get policy on remaining rows (essentially just renormalisation) for next step
            probs = get_policy_probs(impro_output, unacquired)
        else:
            # Do nongreedy loss calculation
            reward_tensor = torch.stack(reward_list)
            for step, logprobs in enumerate(logprob_list):
                # step x batch x 1
                avg_rewards_tensor = torch.mean(reward_tensor, dim=2, keepdim=True)
                # Get number of trajectories for correct average (see Wouter's paper)
                num_traj = logprobs.size(-1)
                # REINFORCE with self-baselines
                # batch x k
                ret = torch.sum(reward_tensor[step:, :, :] - avg_rewards_tensor[step:, :, :], dim=0)
                loss = -1 * (logprobs * ret) / (num_traj - 1)

                # size: batch
                loss = loss.sum(dim=1)
                # scalar, divide by batches_step to mimic taking mean over larger batch
                loss = loss.mean() / args.batches_step
                loss.backward()

        # Average over trajectories, sum over batch dimension
        batch_ssims.append(scores.mean(dim=1).sum().item())

    return batch_ssims


class Arguments:
    def __init__(self, run, accel, steps, sr, traj, mode, batches_step, m_epoch, data_runs, iters, batch_size, force):
        self.accelerations = [accel]
        self.reciprocals_in_center = [1]
        self.acquisition_steps = steps
        self.batch_size = batch_size

        self.data_path = pathlib.Path('/home/timsey/HDD/data/fastMRI/singlecoil')
        self.recon_model_checkpoint = pathlib.Path(
            '/home/timsey/Projects/fastMRI-shi/models/unet/al_nounc_res128_8to4in2_cvol_symk/model.pt')
        self.impro_model_checkpoint = pathlib.Path('/home/timsey/Projects/mrimpro/' + run) / 'model_{}.pt'.format(
            m_epoch)

        self.sample_rate = sr
        self.acquisition = None
        self.center_volume = True
        self.dataset = 'fastmri'
        self.challenge = 'singlecoil'

        self.recon_model_name = 'nounc'
        self.impro_model_name = 'convpool'

        self.device = 'cuda'
        self.num_workers = 8

        self.num_trajectories = traj
        self.batches_step = batches_step
        self.mode = mode

        self.data_runs = data_runs
        self.iters = iters
        self.m_epoch = m_epoch
        self.force_computation = force

        self.use_sensitivity = False

        self.train_state = self.dev_state = self.test_state = None


def main():
    # Greedy
    # 1043
    g_run = 'exp_results/res128_al16_accel[8]_convpool_nounc_k8_2020-05-22_11:43:14'

    # 1046
    # g_run_long = 'exp_results/res128_al28_accel[32]_convpool_nounc_k8_2020-05-22_13:10:25'
    # 1060
    g_run_long = 'exp_results/res128_al28_accel[32]_convpool_nounc_k8_2020-05-27_10:12:00'

    # Non greedy
    # 990
    # ng_run = 'exp_results/res128_al16_accel[8]_convpool_nounc_k8_2020-05-17_12:36:03'
    # 1040
    # ng_run = 'exp_results/res128_al16_accel[8]_convpool_nounc_k8_2020-05-22_03:58:51'
    # 1085
    ng_run = 'exp_results/res128_al16_accel[8]_convpool_nounc_k16_2020-05-29_20:46:15'

    # 1020
    # ng_run_long = 'exp_results/res128_al28_accel[32]_convpool_nounc_k8_2020-05-18_22:05:41'
    # 1049
    # ng_run_long = 'exp_results/res128_al28_accel[32]_convpool_nounc_k8_2020-05-24_11:07:52'
    # 1071
    ng_run_long = 'exp_results/res128_al28_accel[32]_convpool_nounc_k8_2020-05-28_10:09:51'

    # Fixed params
    batch_size = 16
    batches_step = 1  # or 4
    iters = None
    style = 'stoch'
    force = False
    runs = 1

    # mode, traj, m_epoch, data_runs, sr, accel, acquisitions
    jobs = [
        ['greedy', 16, 0, runs, 0.5, 8, 16],
        ['nongreedy', 16, 0, runs, 0.5, 8, 16],
        ['greedy', 16, 0, runs, 0.5, 32, 28],
        ['nongreedy', 16, 0, runs, 0.5, 32, 28],
        ['greedy', 16, 9, runs, 0.5, 8, 16],
        ['nongreedy', 16, 9, runs, 0.5, 8, 16],
        ['greedy', 16, 9, runs, 0.5, 32, 28],
        ['nongreedy', 16, 9, runs, 0.5, 32, 28],
        ['greedy', 16, 19, runs, 0.5, 8, 16],
        ['nongreedy', 16, 19, runs, 0.5, 8, 16],
        ['greedy', 16, 19, runs, 0.5, 32, 28],
        ['nongreedy', 16, 19, runs, 0.5, 32, 28],
        ['greedy', 16, 29, runs, 0.5, 8, 16],
        ['nongreedy', 16, 29, runs, 0.5, 8, 16],
        ['greedy', 16, 29, runs, 0.5, 32, 28],
        ['nongreedy', 16, 29, runs, 0.5, 32, 28],
        ['greedy', 16, 39, runs, 0.5, 8, 16],
        ['nongreedy', 16, 39, runs, 0.5, 8, 16],
        ['greedy', 16, 39, runs, 0.5, 32, 28],
        ['nongreedy', 16, 39, runs, 0.5, 32, 28],
        ['greedy', 16, 49, runs, 0.5, 8, 16],
        ['nongreedy', 16, 49, runs, 0.5, 8, 16],
        ['greedy', 16, 49, runs, 0.5, 32, 28],
        ['nongreedy', 16, 49, runs, 0.5, 32, 28],
    ]

    results_dict = {}
    for i, (mode, traj, m_epoch, data_runs, sr, accel, steps) in enumerate(jobs):
        pr_str = (f"Job {i + 1}/{len(jobs)}\n"
                  f"   mode: {mode:>9}, accel {accel:>2}, steps {steps:>2}\n"
                  f"   ckpt: {m_epoch:>2}, runs: {data_runs:>2}, srate {sr:>3}, traj {traj:>2}")
        print(pr_str)

        if mode == 'greedy':
            if accel == 8 and steps == 16:
                run = g_run
            elif accel == 32 and steps == 28:
                run = g_run_long
            else:
                raise ValueError()

        elif mode == 'nongreedy':
            if accel == 8 and steps == 16:
                run = ng_run
            elif accel == 32 and steps == 28:
                run = ng_run_long
            else:
                raise ValueError()

        else:
            raise ValueError()

        args = Arguments(run, accel, steps, sr, traj, mode, batches_step, m_epoch, data_runs, iters, batch_size, force)

        weight_path, bias_path, param_dir = compute_gradients(args)
        snr = compute_snr(weight_path, bias_path, style)

        summary_dict = {'snr': str(snr),
                        'weight_grads': str(weight_path),
                        'bias_grads': str(bias_path)}

        summary_path = param_dir / f'snr_{style}_summary.json'
        print(f"   Saving summary to {summary_path}")
        with open(summary_path, 'w') as f:
            json.dump(summary_dict, f, indent=4)

        results_dict[i] = {'job': (mode, traj, m_epoch, data_runs, sr, accel, steps),
                           'snr': str(snr)}
        print(f'SNR: {snr}')

    savestr = f'{datetime.datetime.now().strftime("%Y-%m-%d_%H:%M:%S")}.json'
    save_dir = pathlib.Path(os.getcwd()) / f'snr_results_{style}'
    save_dir.mkdir(parents=True, exist_ok=True)
    save_file = save_dir / savestr
    print(f'\nSaving results to: {save_file}')
    with open(save_file, 'w') as f:
        json.dump(results_dict, f, indent=4)

    print('\nFinal results:')
    pprint(results_dict)


if __name__ == "__main__":
    main()
