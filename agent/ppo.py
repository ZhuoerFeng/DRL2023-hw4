from functools import partial
import os
import torch
from torch import nn
import torch.nn.functional as F
import numpy as np
from agent.ddpg import DDPGAgent
from copy import deepcopy
from models import PPOActor, ValueNet
from buffer import PPOReplayBuffer
import logging
logger = logging.getLogger(__name__)


class PPOAgent(DDPGAgent):
    def __init__(self, state_size, action_size, action_space, hidden_dim, lr, gamma,
                 tau, nstep, device, clip_range=0.2, value_clip_range=None,
                 value_coef=0.5, entropy_coef=0.01, update_epochs=10, mini_batch_size=512):

        self.value_net = ValueNet(state_size, hidden_dim, activation=nn.Tanh).to(device)
        self.actor_net = PPOActor(state_size, action_size, hidden_dim, deepcopy(action_space),
                                  activation=nn.Tanh).to(device)

        self.parameters = list(self.actor_net.parameters()) + list(self.value_net.parameters())

        self.optimizer = torch.optim.Adam(self.parameters, lr=lr, eps=1e-5)  # PPO impl. trick
    
        self.tau = tau
        self.gamma = gamma ** nstep
        self.device = device

        self.clip_range = clip_range
        self.value_coef = value_coef
        self.value_clip_range = value_clip_range
        self.entropy_coef = entropy_coef
        self.update_epochs = update_epochs
        self.mini_batch_size = mini_batch_size

        self.train_step = 0

        self.ortho_init()

    def ortho_init(self):
        module_gains = {
            self.actor_net.fcs[:4]: np.sqrt(2),
            self.value_net.fcs[:4]: np.sqrt(2),
            self.actor_net.fcs[4:]: 0.01,
            self.value_net.fcs[4:]: 1,
        }
        for module, gain in module_gains.items():
            module.apply(partial(self.init_weights, gain=gain))

    @staticmethod
    def init_weights(module: nn.Module, gain: float = 1) -> None:
        """
        Taken from stable-baselines 3
        Orthogonal initialization (used in PPO and A2C)
        """
        if isinstance(module, (nn.Linear, nn.Conv2d)):
            nn.init.orthogonal_(module.weight, gain=gain)
            if module.bias is not None:
                module.bias.data.fill_(0.0)

    def __repr__(self):
        return "PPOAgent"

    @torch.no_grad()
    def get_value(self, state, tensor=False):
        ret = self.value_net(torch.as_tensor(state, dtype=torch.float32).to(self.device))
        return ret if tensor else ret.cpu().numpy()
    
    @torch.no_grad()
    def get_action(self, state, sample=True):
        action, _, = self.actor_net(torch.as_tensor(state, dtype=torch.float32).to(self.device), sample=sample)
        return action.cpu().numpy()

    @torch.no_grad()
    def act(self, state, sample=True):
        action, log_prob = self.actor_net(torch.as_tensor(state, dtype=torch.float32).to(self.device), sample=sample)
        return action.cpu().numpy(), log_prob.cpu().numpy()
    
    def get_new_log_prob_entropy_value(self, state, action) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """
        Return log_prob, entropy, value given state and action using both actor and value nets
        """
        ############################
        # YOUR IMPLEMENTATION HERE #
        log_prob, entropy = self.actor_net.evaluate_actions(state, action)
        return log_prob, entropy, self.value_net(state)
        ############################
    
    def get_clipped_surrogate_loss(self, log_prob, old_log_prob, advantage) -> torch.Tensor:
        """
        Return clipped surrogate loss given log_prob, old_log_prob, advantage
        """
        ############################
        # YOUR IMPLEMENTATION HERE #
        log_ratio = log_prob - old_log_prob
        ratio = torch.exp(log_ratio)
        pg_loss1 = advantage * ratio
        pg_loss2 = advantage * torch.clamp(ratio, 1 - self.clip_range, 1 + self.clip_range)
        pg_loss = - torch.mean(torch.min(pg_loss1, pg_loss2))
        return pg_loss
        ############################

    def get_value_loss(self, value, old_value, returns) -> torch.Tensor:
        """
        Return value loss given value, old_value, returns
        """
        # (Optional) If self.value_clip_range is not None, use clipped value loss
        # Otherwise, use MSE loss
        ############################
        # YOUR IMPLEMENTATION HERE #
        if self.value_clip_range is not None:
            value_clipped = torch.clamp(
                value, 
                old_value - self.value_clip_range, 
                old_value + self.value_clip_range)
        else:
            value_clipped = value
        vf_loss1 = (value - returns) ** 2
        vf_loss2 = (value_clipped - returns) ** 2
        vf_loss = torch.min(vf_loss1, vf_loss2)
        return torch.mean(vf_loss)
        ############################

    def get_entropy_loss(self, entropy) -> torch.Tensor:
        """
        Return entropy loss given entropy
        """
        ############################
        # YOUR IMPLEMENTATION HERE #
        return -torch.mean(entropy)
        ############################

    def update_step(self, batch):
        
        state, action, old_log_prob, old_value, advantage, returns = batch

        # normalize advantage
        advantage = (advantage - advantage.mean()) / (advantage.std() + 1e-8)

        log_prob, entropy, value = self.get_new_log_prob_entropy_value(state, action)

        policy_loss = self.get_clipped_surrogate_loss(log_prob, old_log_prob, advantage)

        value_loss = self.get_value_loss(value, old_value, returns)

        entropy_loss = self.get_entropy_loss(entropy)

        # total loss
        loss = policy_loss + self.value_coef * value_loss + self.entropy_coef * entropy_loss

        # optimize and backprop
        self.optimizer.zero_grad()
        loss.backward()
        # clip grad norm
        nn.utils.clip_grad_norm_(self.parameters, 0.5)
        self.optimizer.step()

        self.train_step += 1

        return {'policy_loss': policy_loss.item(),
                'value_loss': value_loss.item(),
                'entropy_loss': entropy_loss.item()}

    def update(self, buffer: PPOReplayBuffer, weights=None):
        policy_losses = []
        value_losses = []
        entropy_losses = []

        buffer_size = buffer.size * buffer.num_envs
        indices = np.arange(buffer_size)
        
        states, actions, old_log_probs, old_values, advantages, returns = buffer.make_dataset()
        
        for e in range(self.update_epochs):
            # random shuffle dataset
            np.random.shuffle(indices)
            for start in range(0, buffer_size, self.mini_batch_size):
                end = start + self.mini_batch_size
                minibatch_idx = indices[start:end]

                batch = (
                    states[minibatch_idx],
                    actions[minibatch_idx],
                    old_log_probs[minibatch_idx],
                    old_values[minibatch_idx],
                    advantages[minibatch_idx],
                    returns[minibatch_idx]
                )
                ret_dict = self.update_step(batch)

                # log losses of final epoch per update
                if e == self.update_epochs - 1:
                    policy_losses.append(ret_dict['policy_loss'])
                    value_losses.append(ret_dict['value_loss'])
                    entropy_losses.append(ret_dict['entropy_loss'])

        return {'policy_loss': np.mean(policy_losses), 
                'value_loss': np.mean(value_losses),
                'entropy_loss': np.mean(entropy_losses)}

    def save(self, name_prefix='best_'):
        os.makedirs('models', exist_ok=True)
        torch.save(self.value_net.state_dict(), os.path.join('models', name_prefix + 'value.pt'))
        torch.save(self.actor_net.state_dict(), os.path.join('models', name_prefix + 'actor.pt'))

    def load(self, name_prefix='best_'):
        self.value_net.load_state_dict(torch.load(os.path.join('models', name_prefix + 'value.pt')))
        self.actor_net.load_state_dict(torch.load(os.path.join('models', name_prefix + 'actor.pt')))