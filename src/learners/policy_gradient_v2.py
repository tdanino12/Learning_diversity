import copy
from components.episode_buffer import EpisodeBatch
from modules.mixers.vdn import VDNMixer
from modules.mixers.qmix import QMixer
from utils.rl_utils import *
import numpy as np
import torch as th
from torch.optim import RMSprop

"""
IAC and VDNs, this class should use ppo agents and ppo mac
"""

class PGLearner_v2:
    def __init__(self, mac, scheme, logger, args):
        self.args = args
        self.n_agents = args.n_agents
        self.n_actions = args.n_actions
        self.mac = mac
        self.logger = logger

        self.last_target_update_step = 0
        self.critic_training_steps = 0

        self.log_stats_t = -self.args.learner_log_interval - 1

        self.target_mac = copy.deepcopy(mac)
        self.params = list(self.mac.parameters())

        if args.mixer is not None:
            if args.mixer == "vdn":
                self.mixer = VDNMixer()
            elif args.mixer == "qmix":
                self.mixer = QMixer(args)
            else:
                raise ValueError("Mixer {} not recognised.".format(args.mixer))
            self.params += list(self.mixer.parameters())

        self.optimiser = RMSprop(params=self.params, lr=args.lr, alpha=args.optim_alpha, eps=args.optim_eps)

    def train(self, batch: EpisodeBatch, t_env: int, episode_num: int):
        # Get the relevant quantities
        bs = batch.batch_size
        max_t = batch.max_seq_length
        rewards = batch["reward"][:, :-1]
        actions = batch["actions"][:, :]
        terminated = batch["terminated"][:, :-1].float()
        mask = batch["filled"][:, :-1].float()
        mask[:, 1:] = mask[:, 1:] * (1 - terminated[:, :-1])
        avail_actions = batch["avail_actions"][:, :]

        critic_mask = mask.clone()
        mask = mask.repeat(1, 1, self.n_agents).view(-1)

        advantages, td_error, targets_taken, log_pi_taken, entropy, hellinger_distance = self._calculate_advs(batch, rewards, terminated, actions, avail_actions,
                                                        critic_mask, bs, max_t)

        pg_loss = - ((advantages.detach() * log_pi_taken) * mask).sum() / mask.sum()
        vf_loss = ((td_error ** 2) * mask).sum() / mask.sum()
        entropy_loss = (entropy * mask).sum() / mask.sum()

        hellinger_loss = hellinger_distance.mean()
        
        coma_loss = pg_loss + self.args.vf_coef * vf_loss + 0.1*hellinger_loss
        if self.args.ent_coef:
            coma_loss -= self.args.ent_coef * entropy_loss

        # Optimise agents
        self.optimiser.zero_grad()
        coma_loss.backward()
        grad_norm = th.nn.utils.clip_grad_norm_(self.params, self.args.grad_norm_clip)
        self.optimiser.step()


        if t_env - self.log_stats_t >= self.args.learner_log_interval:
            self.logger.log_stat("critic_loss", ((td_error ** 2) * mask).sum().item() / mask.sum().item(), t_env)
            self.logger.log_stat("td_error_abs", (td_error.abs() * mask).sum().item() / mask.sum().item(), t_env)
            self.logger.log_stat("q_taken_mean", (targets_taken * mask).sum().item() / mask.sum().item(), t_env)
            self.logger.log_stat("target_mean", ((targets_taken + advantages) * mask).sum().item() / mask.sum().item(), t_env)
            self.logger.log_stat("pg_loss", - ((advantages.detach() * log_pi_taken) * mask).sum().item() / mask.sum().item(), t_env)
            self.logger.log_stat("advantage_mean", (advantages * mask).sum().item() / mask.sum().item(), t_env)
            self.logger.log_stat("coma_loss", coma_loss.item(), t_env)
            self.logger.log_stat("agent_grad_norm", grad_norm, t_env)
            # self.logger.log_stat("pi_max", (pi.max(dim=1)[0] * mask).sum().item() / mask.sum().item(), t_env)
            self.log_stats_t = t_env

    def _calculate_advs(self, batch, rewards, terminated, actions, avail_actions, mask, bs, max_t):
        mac_out = []
        q_outs = []
        # Roll out experiences
        self.mac.init_hidden(batch.batch_size)
        for t in range(batch.max_seq_length):
            agent_out, q_out = self.mac.forward(batch, t=t)
            mac_out.append(agent_out)
            q_outs.append(q_out)
        mac_out = th.stack(mac_out, dim=1)  # Concat over time
        q_outs = th.stack(q_outs, dim=1)  # Concat over time

        # Mask out unavailable actions, renormalise (as in action selection)
        mac_out[avail_actions == 0] = 0
        mac_out = mac_out/mac_out.sum(dim=-1, keepdim=True)
        mac_out[avail_actions == 0] = 0

        # Calculated baseline
        pi = mac_out[:, :-1]  #[bs, t, n_agents, n_actions]
        pi_taken = th.gather(pi, dim=-1, index=actions[:, :-1]).squeeze(-1)    #[bs, t, n_agents]
        
        # "pi_taken" is of dimensions [batch size (num of episodes), num of time step per episode, num of agents]
        hellinger_distance = np.zeros(pi_taken.shape[2])
        count = 0
        for i in range(pi_taken.shape[0]):
            for j in range(pi_taken.shape[1]):
                # Each iteration corresponds to a single time step
                hellinger_distance += self._hellinger_distance(pi_taken[i,j,:])
                count+=1
        hellinger_distance = hellinger_distance/count
        
        action_mask = mask.repeat(1, 1, self.n_agents)
        pi_taken[action_mask == 0] = 1.0
        log_pi_taken = th.log(pi_taken).reshape(-1)

        # Calculate entropy
        entropy = categorical_entropy(pi).reshape(-1)  #[bs, t, n_agents, 1]

        # Calculate q targets
        targets_taken = q_outs.squeeze(-1)   #[bs, t, n_agents]
        if self.args.mixer:
            targets_taken = self.mixer(targets_taken, batch["state"][:, :]) #[bs, t, 1]

        # Calculate td-lambda targets
        targets = build_td_lambda_targets(rewards, terminated, mask, targets_taken, self.n_agents, self.args.gamma, self.args.td_lambda)

        advantages = targets - targets_taken[:, :-1]
        advantages = advantages.unsqueeze(2).repeat(1, 1, self.n_agents, 1).reshape(-1)

        td_error = targets_taken[:, :-1] - targets.detach()
        td_error = td_error.unsqueeze(2).repeat(1, 1, self.n_agents, 1).reshape(-1)


        return advantages, td_error, targets_taken[:, :-1].unsqueeze(2).repeat(1, 1, self.n_agents, 1).reshape(-1), log_pi_taken, entropy, hellinger_distance

    def _hellinger_distance(self, agent_out):
        agent_out_arr = agent_out.detach().numpy()
        num_agents = agent_out_arr.shape[0]
        h_distance = np.zeros(num_agents)  
        for count, arr in enumerate(agent_out_arr):
            new_arr = np.delete(agent_out_arr, count, axis=0)  # delete count_th agent decisions.
            H = self._calc_h_dist(arr,new_arr)
            h_distance[count] = H/(num_agents-1)
        return h_distance   

    def _calc_h_dist(self, agent_actions, other_actions):
        H = 0
        for arr in other_actions:
            H+= np.sqrt(1-np.sqrt(agent_actions*arr))  # element wise multiplication
        return H
        
    def cuda(self):
        self.mac.cuda()
        if self.args.mixer:
            self.mixer.cuda()

    def save_models(self, path):
        self.mac.save_models(path)
        if self.args.mixer:
            th.save(self.mixer.state_dict(), "{}/mixer.th".format(path))
        th.save(self.optimiser.state_dict(), "{}/opt.th".format(path))

    def load_models(self, path):
        self.mac.load_models(path)
        if self.args.mixer:
            self.mixer.load_state_dict(th.load("{}/mixer.th".format(path), map_location=lambda storage, loc: storage))
        self.optimiser.load_state_dict(th.load("{}/opt.th".format(path), map_location=lambda storage, loc: storage))
