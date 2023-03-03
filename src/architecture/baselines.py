import torch
import torch.nn as nn

import src.utils as utils
from src.train import run_episode


class Baseline(nn.Module):
    """The base class for all Baselines."""
    def __init__(self, *args, **kwargs):
        super().__init__()
    
    def forward(self, *args, **kwargs):
        raise NotImplementedError


class BaselineRollout(Baseline):
    """Computes greedy rollout if 'num_rollouts' is -1 or sampled rollout
    if 'num_rollout' > 0."""
    def __init__(self, num_rollouts=-1, *args, **kwargs):
        super().__init__()
        if (type(num_rollouts) != int) or (num_rollouts < -1) or (num_rollouts == 0):
            raise ValueError(f"{num_rollouts} is not a valid number of rollouts, try with -1 for 'greedy' or 1, 2, 3, etc. for 'sampled'.")

        if num_rollouts == -1:
            self.strategy = 'greedy'
            self.num_rollouts = 1
        elif num_rollouts >= 1:
            self.strategy = 'sampled'
            self.num_rollouts = num_rollouts


    def forward(self, formula, num_variables, policy_network,
                device, permute_vars, permute_seed):

        buffer = run_episode(num_variables,
                             policy_network,
                             device,
                             strategy = self.strategy,
                             batch_size = self.num_rollouts,
                             permute_vars = permute_vars,
                             permute_seed = permute_seed)
        
        mean_num_sat = utils.num_sat_clauses_tensor(formula, buffer.action.detach()).mean().detach()
        return mean_num_sat




# class BaselineNet(Baseline):
#     """ """
#     def __init__(self, hidden_size  **kwargs):
#         super().__init__(**kwargs)
#         # Output
#         self.baseline = nn.Linear(hidden_size, 1)

#     def forward(self, formula, num_variables, variables, policy_network, device, dec_state):

#         return self.baseline(dec_state)