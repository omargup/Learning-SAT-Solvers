from random import betavariate
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.distributions as distributions

from src.initializers.state_initializer import TrainableState
import src.utils as utils
from src.utils import sampling_assignment

import numpy as np
from ray import tune

from ray.air import session
from ray.air.checkpoint import Checkpoint

import os
import time
from tqdm import tqdm
import json



# TODO: save only sum, not each value.
class Buffer():
    """
    Tracks episode's relevant information.
    """
    def __init__(self, batch_size, num_variables, dec_output_size, device) -> None:
        # Episode Buffer
        self.action_logits = torch.empty(size=(batch_size, num_variables, dec_output_size)).to(device)
        # buffer_action_logits: [batch_size, seq_len=num_variables, feature_size=1or2]
        self.action_probs = torch.empty(size=(batch_size, num_variables, dec_output_size)).to(device)
        # buffer_action_probs: [batch_size, seq_len=num_variables, feature_size=1or2]
        self.action = torch.empty(size=(batch_size, num_variables), dtype = torch.int64).to(device)
        # buffer_action: [batch_size, seq_len=num_variables]
        self.action_log_prob = torch.empty(size=(batch_size, num_variables)).to(device)
        # buffer_action_log_prob: [batch_size, seq_len=num_variables]
        #self.entropy = torch.empty(size=(batch_size, num_variables)).to(device)
        # buffer_entropy: [batch_size, seq_len=num_variables]
    
    def update(self, idx, t, action_logits, action_probs, action, action_log_prob):
        assert action.dtype == torch.int64, f'action in update Buffer. dtype: {action.dtype}, shape: {action.shape}.'
        self.action_logits[idx, t] = action_logits.squeeze(1)
        self.action_probs[idx, t] = action_probs.squeeze(1)
        self.action[idx, t] = action.view(-1)
        self.action_log_prob[idx, t] = action_log_prob.view(-1)
        #self.entropy[idx, t] = entropy.view(-1)


def run_episode(num_variables,
                policy_network,
                device,
                strategy='sampled',
                batch_size=1,
                permute_vars=False,
                permute_seed=None,  # e.g.: 2147483647 
                logit_clipping=None,  # {None, int >= 1}
                logit_temp=None):  # {None, float >= 1}        
    """
    Runs an episode and returns an updated buffer.
    """
    if logit_clipping is not None:
        if logit_clipping < 1:
            raise ValueError(f"`logit_clipping` must be equal or greater than 1, got {logit_clipping}.")
        C = logit_clipping
        logit_clipping = True
    else:
        logit_clipping = False
    
    if logit_temp is not None:
        if logit_temp < 1:
            raise ValueError(f"`logit_temp` must be equal or greater than 1, got {logit_temp}.")
        T = logit_temp
        logit_temp = True
    else:
        logit_temp = False

    dec_type = policy_network.decoder.decoder_type
    dec_output_size = policy_network.decoder.output_size
    assert (dec_output_size == 1) or (dec_output_size == 2), f"In run_episode. Decoder's output shape[-1]: {dec_output_size}"
    
    buffer = Buffer(batch_size, num_variables, dec_output_size, device)
    batch_idx = [i for i in range(batch_size)]

    permutation = utils.vars_permutation(num_variables,
                                         device,
                                         batch_size,
                                         permute_vars,
                                         permute_seed)
    # permutation: [num_variables, batch_size]
    #   e.g.: [[0, 1, 2, 3, 4], [0, 1, 2, 3, 4]]
    
    state = None
    if (dec_type == "GRU") or (dec_type == "LSTM"):
        
        # Expanding dec_init_state
        # dec_init_state: [num_layers, batch_size=1, hidden_size] 
        if policy_network.dec_init_state is not None:
            if dec_type == "LSTM":
                dim0 = policy_network.dec_init_state[0].shape[0]
                dim1 = batch_size
                dim2 = policy_network.dec_init_state[0].shape[2]
                state = (policy_network.dec_init_state[0].expand(dim0, dim1, dim2).contiguous().to(device), \
                         policy_network.dec_init_state[1].expand(dim0, dim1, dim2).contiguous().to(device))
                # state h: [num_layers, batch_size, hidden_size]
                # state c: [num_layers, batch_size, hidden_size]
            else:  # GRU
                dim0 = policy_network.dec_init_state.shape[0]
                dim1 = batch_size
                dim2 = policy_network.dec_init_state.shape[2]
                state = policy_network.dec_init_state.expand(dim0, dim1, dim2).contiguous().to(device)
                # state: [num_layers, batch_size, hidden_size]
        else:
            state = None

    # One_hot encoding init_action
    # init_action: [batch_size=1, seq_len=1, feature_size=1]
    init_action = F.one_hot(policy_network.init_action.squeeze(-1), 3).float()
    # init_action: [batch_size=1, seq_len=1, features_size=3]

    # Expanding init_action
    # init_action: [batch_size=1, seq_len=1, features_size=3]
    action_prev = init_action.expand(batch_size, 1, init_action.shape[-1]).to(device)
    # action_prev: [batch_size, seq_len=1, features_size=3]

    # Expanding dec_vars
    # dec_vars: [batch_size=1, seq_len=num_vars, feature_size={num_vars, 2*n2v_emb_dim}]
    dec_vars = policy_network.dec_vars.expand(batch_size, num_variables, policy_network.dec_vars.shape[-1])
    # dec_vars: [batch_size, seq_len=num_vars, feature_size={num_vars, 2*n2v_emb_dim}]

    # Expanding dec_context
    # dec_context: [batch_size=1, feature_size={0, 2*n2v_emb_dim}]
    dec_context = policy_network.dec_context.expand(batch_size, num_variables, policy_network.dec_context.shape[-1])
    # dec_context: [batch_size, seq_len=num_vars, feature_size={0, 2*n2v_emb_dim]

    for t, var_idx in enumerate(permutation):
        
        if (dec_type == "GRU") or (dec_type == "LSTM"):
            var_t = dec_vars[batch_idx, var_idx].unsqueeze(1)
            assert var_t.shape == (batch_size, 1, dec_vars.shape[-1])
            # var_t: [batch_size, seq_len=1, feature_size={num_vars, 2*n2v_emb_dim}]

            conx_t = dec_context[batch_idx, var_idx].unsqueeze(1)
            assert conx_t.shape == (batch_size, 1, dec_context.shape[-1])
            # conx_t: [batch_size, seq_len=1, feature_size={0, 2*n2v_emb_dim]

            # Action logits
            action_logits, state = policy_network((var_t.to(device), action_prev, conx_t.to(device)), state)
            assert (action_logits.shape == (batch_size, 1, 1)) or (action_logits.shape == (batch_size, 1, 2)), f'action_logits in run_episode. shape: {action_logits.shape}.'
            # action_logits: [batch_size, seq_len=1, output_size=(1 or 2)]
        
        else:  # transformer
            idx = permutation[:t+1]
            var_t = dec_vars[batch_idx, idx].permute(1,0,2)
            assert var_t.shape == (batch_size, t+1, dec_vars.shape[-1])
            # var_t: [batch_size, seq_len=t+1, feature_size={num_vars, 2*n2v_emb_dim}]

            conx_t = dec_context[batch_idx, idx].permute(1,0,2)
            assert conx_t.shape == (batch_size, t+1, dec_context.shape[-1])
            # conx_t: [batch_size, seq_len=t+1, feature_size={0, 2*n2v_emb_dim]

            mask = policy_network.decoder.generate_square_subsequent_mask(t+1).to(device)

            # Action logits
            action_logits_full = policy_network((var_t.to(device), action_prev, conx_t.to(device)), mask)
            assert (action_logits_full.shape == (batch_size, t+1, 1)) or (action_logits_full.shape == (batch_size, t+1, 2)), f'action_logits_full in run_episode. shape: {action_logits_full.shape}.'
            # action_logits_full: [batch_size, seq_len=t+1, output_size=(1 or 2)]

            action_logits = action_logits_full[:,-1,:].unsqueeze(1)
            assert (action_logits.shape == (batch_size, 1, 1)) or (action_logits.shape == (batch_size, 1, 2)), f'action_logits in run_episode. shape: {action_logits.shape}.'
            # action_logits: [batch_size, seq_len=1, output_size=(1 or 2)]

            #print(f'logits:\n{action_logits_full}')
        
        # Promote exploration
        if logit_clipping:
            action_logits = C * torch.tanh(action_logits)
        if logit_temp:
            action_logits = action_logits / T

        # Prob distribution over actions
        if action_logits.shape[-1] == 1:
            action_probs = torch.sigmoid(action_logits)
            action_dist = distributions.Bernoulli(probs=action_probs)
        else:  # action_logits.shape[-1] == 2
            action_probs = F.softmax(action_logits, dim=-1)
            action_dist = distributions.Categorical(probs=action_probs)
        assert (action_probs.shape == (batch_size, 1, 1)) or (action_probs.shape == (batch_size, 1, 2)), f'action_probs in run_episode. shape: {action_probs.shape}.'
        
        # Action selection
        if strategy == 'greedy':
            if action_logits.shape[-1] == 1:
                action = torch.round(action_probs)
            else:  # action_logits.shape[-1] == 2
                action = torch.argmax(action_probs, dim=-1, keepdim=True)
        elif strategy == 'sampled':
            if action_logits.shape[-1] == 1:
                action = action_dist.sample()
            else:
                action = action_dist.sample().unsqueeze(-1)
        else:
            raise TypeError("{} is not a valid strategy, try with 'greedy' or 'sampled'.".format(strategy))
        assert action.shape == (batch_size, 1, 1), f"action.shape: {action.shape}, strategy: {strategy} , dec's output: {dec_output_size}"
        # action: [batch_size, seq_len=1, feature_size=1]

        # Log-prob of the action
        if action_logits.shape[-1] == 1:
            action_log_prob = action_dist.log_prob(action)
        else:
            action_log_prob = action_dist.log_prob(action.squeeze(-1)).unsqueeze(-1)

        # Computing Entropy
        #if action_logits.shape[-1] == 1:
        #    entropy = action_dist.entropy()
        #else:
        #    entropy = action_dist.entropy().unsqueeze(-1)
        
        # Take the choosen action
        #-------

        # Update buffer
        buffer.update(batch_idx, var_idx, action_logits, action_probs, action.to(dtype=torch.int64), action_log_prob)
        
        #actions_logits.append(list(np.around(action_logits.detach().cpu().numpy().flatten(), 2)))
        #actions_softmax.append(list(np.around(F.softmax(action_logits.detach(), -1).numpy().flatten(), 2)))
        #actions.append(action.item())
        #action_log_probs.append(action_log_prob)
        #entropies.append(action_dist_entropy)

        action = action.to(dtype=torch.int64)
        # action: [batch_size, seq_len=1, feature_size=1]
        if (dec_type == "GRU") or (dec_type == "LSTM"):
            # action: [batch_size, seq_len=1, feature_size=1]
            action_prev = F.one_hot(action.squeeze(-1), 3).float()
            # action_prev: [batch_size, seq_len=1, features_size=3]
            assert action_prev.shape == torch.Size([batch_size, 1, 3])
        
        else:  # transformer
            action = F.one_hot(action.squeeze(-1), 3).float()
            # action: [batch_size, seq_len=1, features_size=3]
            action_prev = torch.cat((action_prev, action), 1)
            # action_prev: [batch_size, seq_len=t+2, features_size=3]
            assert action_prev.shape == torch.Size([batch_size, t+2, 3])

    #print(f'logits:\n{action_logits_full}')
    #print(f'logits:\n{buffer.action_logits}')
    #print(f'probs: \n{buffer.action_probs}')
    #print(f'actions: \n{buffer.action}')
    
    # return buffer.action  # self.buffer.action.detach().cpu().numpy()
    return buffer 


def train(formula,
          num_variables, 
          policy_network,
          optimizer,
          device,
          batch_size=1,
          permute_vars=False,
          permute_seed=None,
          baseline=None,
          logit_clipping=None,  # {None, int >= 1}
          logit_temp=None,  # {None, float >= 1}
          entropy_estimator='crude',  # {'crude', 'smooth'}
          beta_entropy=0,  
          clip_grad=None,  # {None, float}
          num_samples=15000,
          accumulation_episodes=1,
          log_interval=100,
          eval_interval=100,
          eval_strategies=[0, 10], # 0 for greedy, i < 0 takes i samples and returns the best one.
          writer = None,  # Tensorboard writer
          extra_logging = False,
          raytune = False,
          run_name = None, 
          save_dir = 'outputs',
          early_stopping= False,
          patience=5,
          entropy_value=0.01,
          verbose=1):
    """ Train a parametric policy following Policy Gradient Theorem
    
    ARGUMENTS
    ----------
        formula: list.
        num_variables: int, number of variables in the formula.
        variables
        policy_network: nn.Module.
        optimizer: torch.optimizer
        device: torch.device.  
        formula_emb:
        batch_size: int.
        permute_vars: bool, {True, False}.
        permute_seed: long,
        baseline:
        entropy_weight:
        clip_grad:
        num_episodes
        accumulation_episodes
        log_interval: int. Log info every `log_interval` episodes. Default: 100.
        eval_interval: int. Run evaluation every `eval_interval` episodes. Default: 100.
        eval_strategies
        writer
        extra_logging
        raytune
        run_name
        dimacs_dir
        early_stopping
        patience
        entropy_value
        verbose
    
    RETURNS
    --------
        active_search: dic. 
    """
    if raytune:
        report_dict = {}
        verbose = 0
    
    if verbose == 0:
        progress_bar = False
    elif (verbose == 1) or (verbose == 2):
        progress_bar = True
    else:
        raise ValueError(f'Verbose must be 0, 1, or 2, got {verbose}.')
    
    # Number of parameters
    total_params = sum(p.numel() for p in policy_network.parameters())
    trainable_params = sum(p.numel() for p in policy_network.parameters() if p.requires_grad)
        
    if verbose > 0:
        print("\n")
        print(policy_network) 
        print(f"\nTotal params: {total_params}")
        print(f"\nTrainable params: {trainable_params}")
        print(f"\nStart training for run-id {run_name}")

    # Put model in train mode
    policy_network.to(device)
    policy_network.train()
    optimizer.zero_grad()

    # Active search solution
    active_search = {'episode': 0,
                     'samples': 0,
                     'num_sat': 0,
                     'strategy': None,
                     'sol': None,
                     'total_episodes': 0,
                     'total_samples': 0,
                     'trainable params': trainable_params}
    
    num_episodes = int(np.ceil(num_samples / batch_size))
    for episode in tqdm(range(1, num_episodes + 1), disable=not progress_bar, ascii=True):

        current_samples = episode * batch_size
        
        buffer = run_episode(num_variables=num_variables,
                             policy_network=policy_network,
                             device=device,
                             strategy='sampled',
                             batch_size=batch_size,
                             permute_vars=permute_vars,
                             permute_seed=permute_seed,
                             logit_clipping=logit_clipping,  # {None, int >= 1}
                             logit_temp=1)  # {None, float >= 1}  
        
        policy_network.eval()
        with torch.no_grad():
            # Compute num of sat clauses
            #num_sat = utils.num_sat_clauses_tensor(formula, buffer.action.detach().cpu().numpy()).detach()
            num_sat = utils.num_sat_clauses_tensor(formula, buffer.action.detach()).detach()
            # num_sat: [batch_size]

            # Compute baseline
            baseline_val = torch.tensor(0, dtype=float).detach().to(device)
            if baseline is not None:
                baseline_val = baseline(formula=formula,
                                        num_variables=num_variables,
                                        policy_network=policy_network,
                                        device=device,
                                        permute_vars=permute_vars,
                                        permute_seed=permute_seed,
                                        logit_clipping=logit_clipping,
                                        logit_temp=logit_temp,
                                        num_sat=num_sat).detach()

        policy_network.train()

        # Update entropy weight
        #w_entropy = entropy_decay.update_w()
        #w_entropy = 0

        # Entropy
        if entropy_estimator == "crude":
            log_prob_a = buffer.action_log_prob
            # log_prob_a: [batch_size, seq_len=num_variables]
            H = - log_prob_a.sum(dim=-1)
            # H: [batch_size]

        elif entropy_estimator == "smooth":
            probs = buffer.action_probs
            # probs: [batch_size, seq_len=num_variables, feature_size={1,2}]
            if probs.shape[-1] == 1:
                probs = torch.cat([probs, 1-probs], dim=-1)
            # probs: [batch_size, seq_len=num_variables, feature_size=2]
            log_probs = torch.log(probs)
            # log_probs: [batch_size, seq_len=num_variables, feature_size=2]
            H = -torch.mul(probs, log_probs).sum(-1).sum(-1)
            # H: [batch_size]
        else:
            raise ValueError(f"{entropy_estimator} is not a valid entropy estimator, try with 'crude' or'smooth'.")

        # Loss (mean over batch)
        # buffer.action_log_prob: [batch_size, seq_len=num_variables]
        log_prob = buffer.action_log_prob.sum(dim=-1)
        # log_prob: [batch_size]
        #print("Devices:")
        #print(num_sat.get_device(), baseline_val.get_device(), log_prob.get_device(), H.get_device())
        pg_loss = ((num_sat.to(device) - baseline_val.to(device)) * log_prob + (beta_entropy * H)).mean()
        # Normalize loss for gradient accumulation
        loss = pg_loss / accumulation_episodes

        # Gradient accumulation
        loss.backward()

        # Perform optimization step after accumulating gradients
        if (episode % accumulation_episodes) == 0:
            if clip_grad is not None:
                nn.utils.clip_grad_norm_(policy_network.parameters(), clip_grad) 
            optimizer.step()
            optimizer.zero_grad()
        
        # Logging
        if (episode % log_interval) == 0:
            num_sat_mean = num_sat.mean().item()
            log_prob_mean = log_prob.mean().item()
            H_mean = H.mean().item()

            # Log values to screen
            if verbose > 0:
                print(f'\nEpisode: {episode}, samples: {current_samples}, num_sat: {num_sat_mean}')
                print('\tpg_loss: ({} - {}) * {} + ({} * {}) = {}'.format(num_sat_mean,
                                                                            baseline_val.item(),
                                                                            log_prob_mean,
                                                                            beta_entropy,
                                                                            H_mean,
                                                                            pg_loss.item()))
            
            if verbose == 2:
                print(f'logits: \n{buffer.action_logits}')
                print(f'probs: \n{buffer.action_probs}')

            if writer is not None:
                writer.add_scalar('num_sat', num_sat_mean, current_samples, new_style=True)
                writer.add_scalar('baseline', baseline_val.item(), current_samples, new_style=True)
                writer.add_scalar('log_prob', log_prob_mean, current_samples, new_style=True)

                writer.add_scalar('pg_loss', (num_sat_mean - baseline_val.item()) * log_prob_mean, current_samples, new_style=True)
                writer.add_scalar('pg_loss_with_ent', (num_sat_mean - baseline_val.item()) * log_prob_mean + (beta_entropy * H_mean), current_samples, new_style=True)
                
                writer.add_scalar('entropy/beta', beta_entropy, current_samples, new_style=True)
                writer.add_scalar('entropy/entropy', H_mean, current_samples, new_style=True)
                writer.add_scalar('entropy/beta*entropy', beta_entropy * H_mean, current_samples, new_style=True)

                if extra_logging:
                    writer.add_histogram('histogram/action_logits', buffer.action_logits, current_samples)
                    writer.add_histogram('histogram/action_probs', buffer.action_probs, current_samples)
                    
                    if policy_network.decoder.decoder_type == "GRU" or policy_network.decoder.decoder_type == "LSTM":
                        if policy_network.decoder.trainable_state:
                            writer.add_histogram('params/init_state', policy_network.decoder.init_state, current_samples)


        # Evaluation
        if (episode % eval_interval) == 0:
            if verbose > 0:
                print('-------------------------------------------------')
                print(f'Evaluation in episode: {episode}. Num samples: {current_samples}. Num of sat clauses:')
            policy_network.eval()
            with torch.no_grad():
                
                for strat in eval_strategies:
                    if (strat < 0) or (type(strat) != int):
                        raise ValueError(f'Values in `eval_strategy` must be 0 if greedy or an integer greater or equal than 1 if sampled, got {strat}.')
                    buffer = run_episode(num_variables = num_variables,
                                         policy_network = policy_network,
                                         device = device,
                                         strategy = 'greedy' if strat == 0 else 'sampled',
                                         batch_size = 1 if strat == 0 else strat,
                                         permute_vars = permute_vars,
                                         permute_seed = permute_seed,
                                         logit_clipping=logit_clipping,  # {None, int >= 1}
                                         logit_temp=logit_temp)  # {None, float >= 1}  )
            
                    # Compute num of sat clauses
                    num_sat = utils.num_sat_clauses_tensor(formula, buffer.action.detach()).detach()
                    # ::num_sat:: [batch_size]

                    # Log values to screen
                    if strat == 0:
                        number_of_sat = num_sat.item()
                        if verbose > 0:
                            print(f'\tGreedy: {number_of_sat}.')
                        if raytune:
                            report_dict['num_sat_greedy'] = number_of_sat
                        
                    
                    else:
                        number_of_sat = num_sat.max().item()
                        if verbose > 0:
                            print(f'\tBest of {strat} samples: {number_of_sat}.')
                        if raytune:
                            report_dict[f'num_sat_sample_{str(strat)}'] = number_of_sat
                    
                    # Keep tracking the active search solution
                    if number_of_sat > active_search['num_sat']:
                        active_search['num_sat'] = number_of_sat
                        active_search['episode'] = episode
                        active_search['samples'] = current_samples
                        active_search['strategy'] = f"{'greedy' if strat == 0 else 'sampled'}{'' if strat == 0 else '-'+str(strat)}"

                        if strat == 0:
                            active_search['sol'] = buffer.action.detach().tolist()
                        else:
                            idx = num_sat.argmax().item()
                            active_search['sol'] = buffer.action[idx].detach().tolist()    

                    if writer is not None:
                        writer.add_scalar(f"eval/{'greedy' if strat == 0 else 'sampled'}{'' if strat == 0 else '-'+str(strat)}",
                                          number_of_sat, current_samples, new_style=True)
                        
                        if extra_logging:
                            dec_output_size = policy_network.decoder.dense_out.bias.shape[0]  # decoder's output can be of size 1 or 2.
                            if strat == 0:
                                writer.add_scalars('eval_buffer/actions', {f'x[{i}]': buffer.action[0][i] for i in range(num_variables)}, current_samples)
                                for out in range(dec_output_size):
                                    writer.add_scalars('eval_buffer/logits', {f'x[{i},{out}]': buffer.action_logits[0][i][out] for i in range(num_variables)}, current_samples)  # batch0, var_i, unormalized p(x_i)
                                    writer.add_scalars('eval_buffer/probs', {f'x[{i},{out}]': buffer.action_probs[0][i][out] for i in range(num_variables)}, current_samples) # batch0, var_i, p(x_i)
                                
                            else:
                                idx = num_sat.argmax().item()
                                writer.add_scalars('eval_buffer/actions', {f'x[{i}]': buffer.action[idx][i] for i in range(num_variables)}, current_samples)
                                for out in range(dec_output_size):
                                    writer.add_scalars('eval_buffer/logits', {f'x[{i},{out}]': buffer.action_logits[idx][i][out] for i in range(num_variables)}, current_samples)  # batch idx, var_i, unormalized p(x_i)
                                    writer.add_scalars('eval_buffer/probs', {f'x[{i},{out}]': buffer.action_probs[idx][i][out] for i in range(num_variables)}, current_samples) # batch idx, var_i, p(x_i)
            
            # Saving the best solution so far
            active_search['total_samples'] = current_samples
            active_search['total_episodes'] = episode
            with open(os.path.join(save_dir, "solution.json"), 'w') as f:
                json.dump(active_search, f, indent=4)
            
            if raytune:
                # episode, samples, num_sat_greedy, num_sat_sample_k
                report_dict['episode'] = episode
                report_dict['samples'] = current_samples        
                session.report(report_dict)#,
                            #checkpoint=checkpoint)

            policy_network.train()

            if verbose > 0:
                print(f"\tActive search: {active_search['num_sat']}.")
                print('-------------------------------------------------\n')
            if writer is not None:
                writer.add_scalar('active_search', active_search['num_sat'], current_samples, new_style=True)

        #if mean_entropy.item() <= entropy_value:
        #    patience_counter += 1

        #m=len(formula)
        if (current_samples == num_samples): # or (active_search['num_sat'] == m): # or (early_stopping and (patience_counter >= patience)):
            criteria = 'Maximum number of episodes reached'

            # elif active_search['num_sat'] == len(formula):
            #     criteria = 'All clauses have been satisfied'
            # else:
            #     criteria = 'Early stoping'
            
            if verbose > 0:
                print('-------------------------------------------------')
                print(f'Optimization process finished at episode: {episode}.')
                print(f'Number of samples: {current_samples}.')
                print(f'Number or trainable parameters: {trainable_params}.')
                print(f'Stop creiteria: {criteria}.')
                print(f"Active search results:")
                print(f"\tNum_sat: {active_search['num_sat']}")
                print(f"\tSamples: {active_search['samples']}")
                print(f"\tEpisode: {active_search['episode']}")
                print(f"\tStrategy: {active_search['strategy']}")
                print("\tSol:")
                print(active_search['sol'])
                print('-------------------------------------------------\n')
            
            break

    return active_search





        # for prob in buffer.action_probs[0]:
        #     writer.add_scalar('prob', prob.item(), episode, new_style=True)

        # if dec_output_size == 1:

        # else:
        #     writer.add_scalars('probs', {'var'+str(i): probs[i] for i in range(5)}, episode, new_style=True)

        # for i in range(num_variables):
        #     write.add_scalar('probs/var'+str(i), probs[i], episode, new_style=True)
        #     write.add_scalar('probs/var'+str(i), probs[i], episode, new_style=True)


            # if raytune:
            #     #with tune.checkpoint_dir(episode) as checkpoint_dir:
            #     #    path = os.path.join(checkpoint_dir, "checkpoint")
            #     #    torch.save((policy_network.state_dict(), optimizer.state_dict()), path)

            #     ray_checkpoints_path = os.path.join('ray_results/checkpoints')
            #     if not os.path.exists(ray_checkpoints_path):
            #         os.makedirs(ray_checkpoints_path)

            #     #path = "/Users/omargutierrez/Documents/Code/learning_sat_solvers/my_model"
            #     #os.makedirs("my_model", exist_ok=True)
            #     torch.save((policy_network.state_dict(), optimizer.state_dict()), 
            #                 os.path.join(ray_checkpoints_path, "checkpoint.pt"))
            #     checkpoint = Checkpoint.from_directory(ray_checkpoints_path)
                

            #     session.report({'optim_step':optim_step, 
            #                     'episode':episode,
            #                     'loss':mean_loss,
            #                     'num_sat':mean_num_sat,
            #                     'num_sat_val':val_num_sat},
            #                     checkpoint=checkpoint)

    
 
