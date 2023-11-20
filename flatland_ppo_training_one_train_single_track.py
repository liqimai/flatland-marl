import argparse
import os
import random
import time
from distutils.util import strtobool

import numpy as np
import torch
from torch import nn
from torch import optim
from torch.utils.tensorboard import SummaryWriter
from tensordict.nn import TensorDictModule
from tensordict.tensordict import TensorDict

from flatland.envs.line_generators import SparseLineGen
from flatland.envs.malfunction_generators import (
    MalfunctionParameters,
    ParamMalfunctionGen,
)
from flatland.envs.rail_env import RailEnv
from flatland.envs.rail_generators import SparseRailGen
from flatland.envs.step_utils.states import TrainState
from flatland.utils.rendertools import AgentRenderVariant, RenderTool
from IPython.display import clear_output
from matplotlib import pyplot as plt

from flatland_cutils import TreeObsForRailEnv as TreeCutils
from impl_config import FeatureParserConfig as fp
from solution.nn.net_tree import Network_td

from gym.vector import SyncVectorEnv

def parse_args():
    # fmt: off
    parser = argparse.ArgumentParser()
    parser.add_argument("--exp-name", type=str, default='one_train_single_track',
        help="the name of this experiment")
    parser.add_argument("--seed", type=int, default=1,
        help="seed of the experiment")
    parser.add_argument("--torch-deterministic", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
        help="if toggled, `torch.backends.cudnn.deterministic=False`")
    parser.add_argument("--cuda", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
        help="if toggled, cuda will be enabled by default")
    parser.add_argument("--track", type=lambda x: bool(strtobool(x)), default=False, nargs="?", const=True,
        help="if toggled, this experiment will be tracked with Weights and Biases")
    parser.add_argument("--wandb-project-name", type=str, default="cleanRL",
        help="the wandb's project name")
    parser.add_argument("--wandb-entity", type=str, default=None,
        help="the entity (team) of wandb's project")
    parser.add_argument("--capture-video", type=lambda x: bool(strtobool(x)), default=False, nargs="?", const=True,
        help="whether to capture videos of the agent performances (check out `videos` folder)")

    # Algorithm specific arguments
    parser.add_argument("--env-id", type=str, default="flatland-rl",
        help="the id of the environment")
    parser.add_argument("--num-agents", type= int, default = 1,
        help="number of agents in the environment")
    parser.add_argument("--total-timesteps", type=int, default=100000,
        help="total timesteps of the experiments")
    parser.add_argument("--learning-rate", type=float, default=2.5e-4,
        help="the learning rate of the optimizer")
    parser.add_argument("--num-envs", type=int, default=1,
        help="the number of parallel game environments")
    parser.add_argument("--num-steps", type=int, default=1000,
        help="the number of steps to run in each environment per policy rollout")
    parser.add_argument("--anneal-lr", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
        help="Toggle learning rate annealing for policy and value networks")
    parser.add_argument("--gamma", type=float, default=0.99,
        help="the discount factor gamma")
    parser.add_argument("--gae-lambda", type=float, default=0.95,
        help="the lambda for the general advantage estimation")
    parser.add_argument("--num-minibatches", type=int, default=20,
        help="the number of mini-batches")
    parser.add_argument("--update-epochs", type=int, default=4,
        help="the K epochs to update the policy")
    parser.add_argument("--norm-adv", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
        help="Toggles advantages normalization")
    parser.add_argument("--clip-coef", type=float, default=0.2,
        help="the surrogate clipping coefficient")
    parser.add_argument("--clip-vloss", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
        help="Toggles whether or not to use a clipped loss for the value function, as per the paper.")
    parser.add_argument("--ent-coef", type=float, default=0.000000001,
        help="coefficient of the entropy")
    parser.add_argument("--vf-coef", type=float, default=0.00001,
        help="coefficient of the value function")
    parser.add_argument("--max-grad-norm", type=float, default=0.5,
        help="the maximum norm for the gradient clipping")
    parser.add_argument("--target-kl", type=float, default=None,
        help="the target KL divergence threshold")
    parser.add_argument("--use-pretrained-network", type=bool, default=True, 
        help="use a trained network from the paper")
    parser.add_argument("--pretrained-network-path", type=str, default = "solution/policy/phase-III-50.pt",
        help="path to the pretrained network to be used")
    parser.add_argument("--do-training", type=bool, default=True,
        help="Toggles whether to train the network or keep it fixed")
    parser.add_argument("--do-render", type=bool, default=False,
        help="Toggles whether to render rollouts in realtime")
    args = parser.parse_args()
    args.batch_size = int(args.num_envs * args.num_steps)
    args.minibatch_size = int(args.batch_size // args.num_minibatches)
    # fmt: on
    return args

class RailEnvTd(RailEnv):
    ''' Custom version of default flatland rail env that changes in- and outputs to tensordicts
    
    Methods:
        - obs_to_dt: Return the list of observations given by the envirnoment as a tensordict
        - reset: Extend the default flatland reset method by returning a tensordict
        - step: Extend the default flatland step by accepting and returning a tensordict
        - update_step_reward: Override default flatland update_step_reward, allowing for custom reward functions upon step completion
    '''
    
    def obs_to_td(self, obs_list):
        ''' Return the observation as a tensordict.'''
        obs_td = TensorDict(
            {
                "agents_attr": torch.tensor(
                    obs_list[0], dtype=torch.float32
                ),
                "node_attr": torch.tensor( # change name from forest in demo plfActor.py
                    obs_list[1][0], dtype=torch.float32
                ),
                "adjacency": torch.tensor(
                    obs_list[1][1], dtype=torch.int64
                ),
                "node_order": torch.tensor(
                    obs_list[1][2], dtype=torch.int64
                ),
                "edge_order": torch.tensor(
                    obs_list[1][3], dtype=torch.int64
                ),
            },
            [self.get_num_agents()],
        )
        return obs_td

    def reset(self, tensordict=None):
        ''' Extend default flatland reset by returning a tensordict. '''
        # get observations
        observations, _ = super().reset()
        if tensordict is None:
            tensordict_out = TensorDict({}, batch_size=[])
        tensordict_out["observations"] = self.obs_to_td(observations)
        # get valid actions
        (
            _,
            _,
            valid_actions,
        ) = self.obs_builder.get_properties()
        tensordict_out["observations"]["valid_actions"] = torch.tensor(
            valid_actions, dtype=torch.bool
        ) 
        #tensordict_out["done"] = torch.tensor(False).type(torch.bool) # not done since just initialized
        return tensordict_out

    def step(self, tensordict):
        '''Extend default flatland step by returning a tensordict. '''
        actions = {
            handle: action.item()
            for handle, action in enumerate(tensordict["actions"])
        }
        observations, rewards, done, _ = super().step(actions)
        (
            _,
            _,
            valid_actions,
        ) = self.obs_builder.get_properties()
        observation_td = TensorDict({}, batch_size=[])
        rewards_td = TensorDict({}, batch_size=[])
        observation_td["observations"] = self.obs_to_td(observations)
        rewards_td["rewards"] = torch.tensor(
            [value for _, value in rewards.items()]
        )
        observation_td["done"] = torch.tensor(done["__all__"]).type(torch.bool)
        observation_td["observations"]["valid_actions"] = torch.tensor(
            valid_actions, dtype=torch.bool
        )
        return observation_td, rewards_td

    def update_step_rewards(self, i_agent):
        reward = None
        agent = self.agents[i_agent]
        # agent done? (arrival_time is not None)
        if agent.state == TrainState.DONE:
            # if agent arrived earlier or on time = 0
            # if agent arrived later = -ve reward based on how late
            reward = min(agent.latest_arrival - agent.arrival_time, 0)

        # Agents not done (arrival_time is None)
        else:
            # CANCELLED check (never departed)
            if agent.state.is_off_map_state():
                reward = (
                    -1
                    * self.cancellation_factor
                    * (
                        agent.get_travel_time_on_shortest_path(
                            self.distance_map
                        )
                        + self.cancellation_time_buffer
                    )
                )

            # Departed but never reached
            if agent.state.is_on_map_state():
                reward = agent.get_current_delay(
                    self._elapsed_steps, self.distance_map
                )
        self.rewards_dict[i_agent] += reward


def create_random_env():
    """Create a random railEnv object
    Taken from the flatland-marl demo
    """

    return RailEnvTd(
        number_of_agents=args.num_agents,
        width=30,  # try smaller environment for hopefully faster learning
        height=35,
        rail_generator=SparseRailGen(
            max_num_cities=3,
            grid_mode=False,
            max_rails_between_cities=2,
            max_rail_pairs_in_city=2,
        ),
        line_generator=SparseLineGen(
            speed_ratio_map={1.0: 1 / 4, 0.5: 1 / 4, 0.33: 1 / 4, 0.25: 1 / 4}
        ),
        malfunction_generator=ParamMalfunctionGen(
            MalfunctionParameters(
                malfunction_rate=1 / 4500, min_duration=20, max_duration=50
            )
        ),
        obs_builder_object=TreeCutils(
            fp.num_tree_obs_nodes, fp.tree_pred_path_depth
        ),
    )

if __name__ == "__main__":
    args = parse_args()

    run_name = (
        f"{args.env_id}__{args.exp_name}__{args.seed}__{int(time.time())}"
    )
    if args.track:
        import wandb

        wandb.init(
            project=args.wandb_project_name,
            entity=args.wandb_entity,
            sync_tensorboard=True,
            config=vars(args),
            name=run_name,
            monitor_gym=True,
            save_code=True,
        )
    writer = SummaryWriter(f"runs/{run_name}")
    writer.add_text(
        "hyperparameters",
        "|param|value|\n|-|-|\n%s"
        % "\n".join([f"|{key}|{value}|" for key, value in vars(args).items()]),
    )

    # TRY NOT TO MODIFY: seeding
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.backends.cudnn.deterministic = args.torch_deterministic

    device = torch.device(
        "cuda" if torch.cuda.is_available() and args.cuda else "cpu"
    )
    print("device used: {}".format(device))

    env = create_random_env()
    
    if args.do_render:
        env_renderer = RenderTool(
            env,
            agent_render_variant=AgentRenderVariant.ONE_STEP_BEHIND,
            show_debug=False,
            screen_height=600,  # Adjust these parameters to fit your resolution
            screen_width=800,
        )

    network = Network_td()
    
    if args.use_pretrained_network:
        model_path = "solution/policy/phase-III-50.pt"
        loaded_model = torch.load(args.pretrained_network_path, map_location=torch.device(device))
        network.load_state_dict(loaded_model)

    td_module = TensorDictModule(
        network,
        in_keys=["observations", "actions"],
        out_keys=["actions", "logprobs", "entropy", "values"],
    ).to(device)
    
    if not args.do_training:
        for param in td_module.parameters():
            param.requires_grad = False

    optimizer = optim.Adam(
        td_module.parameters(), lr=args.learning_rate, eps=1e-5
    )

    #initialize storage for the rollouts
    observations = TensorDict(
        {
            "agents_attr": torch.zeros(
                args.num_steps, args.num_agents, 83, dtype=torch.float32
            ),
            "node_attr": torch.zeros(
                args.num_steps, args.num_agents, 31, 12, dtype=torch.float32
            ),
            "adjacency": torch.zeros(
                args.num_steps, args.num_agents, 30, 3, dtype=torch.int64
            ),
            "node_order": torch.zeros(
                args.num_steps, args.num_agents, 31, dtype=torch.int64
            ),
            "edge_order": torch.zeros(
                args.num_steps, args.num_agents, 30, dtype=torch.int64
            ),
            "valid_actions": torch.zeros(
                args.num_steps, args.num_agents, *env.action_space, dtype=torch.bool
            ),
        },
        batch_size=(args.num_steps, args.num_agents),
    )

    actions_init = torch.zeros((args.num_steps, args.num_agents), dtype=torch.int64)
    logprobs = torch.zeros((args.num_steps, args.num_agents), dtype=torch.float32)
    entropy = torch.zeros((args.num_steps), dtype = torch.float32)
    rewards = torch.zeros((args.num_steps, args.num_agents), dtype=torch.float32)
    done = torch.zeros((args.num_steps), dtype=torch.bool)
    values = torch.zeros((args.num_steps, args.num_agents), dtype=torch.float32)

    rollout_data = TensorDict(
        {
            "observations": observations,
            "actions": actions_init,
            "logprobs": logprobs,
            'entropy': entropy,
            "rewards": rewards,
            "done": done,
            "values": values,
        },
        batch_size=[args.num_steps],
    )

    # TRY NOT TO MODIFY: start the game
    global_step = 0
    start_time = time.time()
    next_obs = env.reset()
    rollout_data = rollout_data.to(device)
    num_updates = args.total_timesteps // args.batch_size

    
    for update in range(1, num_updates + 1):
        # Annealing the rate if instructed to do so.
        if args.anneal_lr:
            frac = 1.0 - (update - 1.0) / num_updates
            lrnow = frac * args.learning_rate
            optimizer.param_groups[0]["lr"] = lrnow

        rollout_data['actions'] = torch.zeros_like(rollout_data['actions'])
        for step in range(0, args.num_steps):
            global_step += 1 * args.num_envs
            rollout_data[
                step
            ].update_(next_obs)# save for training, also includes the done
            # ALGO LOGIC: action logic
            with torch.no_grad():
                rollout_data[[step]] = (td_module(rollout_data[[step]]))

            next_obs, rewards= env.step(rollout_data[step])
            rollout_data[step].update_(rewards) # save the rewards received for actions in current step
            
            if next_obs['done']:
                next_obs.update_(env.reset()) # only overwriting keys returned by reset, i.e. 'observations'
                if args.do_render:
                    env_renderer.reset()

            if args.do_render:
                env_renderer.render_env(show=True, show_observations=False, show_predictions=False)

        writer.add_scalar(
            "rewards/min", rollout_data["rewards"].min(), global_step
        )
        writer.add_scalar(
            "rewards/mean", rollout_data["rewards"].mean(), global_step
        )
        next_obs = next_obs.to(device)
        next_obs['actions'] = torch.ones_like(rollout_data[0]['actions'])
        # Calculate advantages
        with torch.no_grad():
            next_value = td_module(next_obs.unsqueeze(0))["values"]
 
            advantages = torch.zeros_like(rollout_data['rewards']).to(device)
            lastgaelam = torch.zeros(args.num_agents).to(device)
            for t in reversed(range(args.num_steps)):
                if t == args.num_steps - 1:
                    nextnonterminal = 1.0 - next_obs['done'].item()
                    nextvalues = next_value
                else:
                    nextnonterminal = 1.0 - rollout_data["done"][t + 1].item()
                    nextvalues = rollout_data["values"][t + 1]
                delta = (
                    rollout_data["rewards"][t]
                    + args.gamma * nextvalues * nextnonterminal
                    - rollout_data["values"][t]
                )
                advantages[t] = lastgaelam = (
                    delta
                    + args.gamma
                    * args.gae_lambda
                    * nextnonterminal
                    * lastgaelam
                )
            returns = advantages + rollout_data["values"]

        # Optimizing the policy and value network
        b_inds = np.arange(args.num_steps)
        clipfracs = []
        for epoch in range(args.update_epochs):
            np.random.shuffle(b_inds)
            for start in range(0, args.batch_size, args.minibatch_size):
                end = start + args.minibatch_size
                mb_inds = b_inds[start:end]

                updated_rollout_data = td_module(rollout_data[mb_inds])

                logratio = (
                    updated_rollout_data["logprobs"]
                    - rollout_data["logprobs"][mb_inds]
                )
                ratio = logratio.exp()

                with torch.no_grad():
                    # calculate approx_kl http://joschu.net/blog/kl-approx.html
                    old_approx_kl = (-logratio).mean()
                    approx_kl = ((ratio - 1) - logratio).mean()
                    clipfracs += [
                        ((ratio - 1.0).abs() > args.clip_coef)
                        .float()
                        .mean()
                        .item()
                    ]

                mb_advantages = advantages[mb_inds]
                if args.norm_adv:
                    mb_advantages = (mb_advantages - mb_advantages.mean()) / (
                        mb_advantages.std() + 1e-8
                    )

                # Policy loss
                pg_loss1 = -mb_advantages * ratio
                pg_loss2 = -mb_advantages * torch.clamp(
                    ratio, 1 - args.clip_coef, 1 + args.clip_coef
                )
                pg_loss = torch.max(pg_loss1, pg_loss2).mean()

                # Value loss
                newvalue = updated_rollout_data["values"]
                if args.clip_vloss:
                    # not adapted for flatland yet
                    v_loss_unclipped = (newvalue - returns[mb_inds]) ** 2
                    v_clipped = rollout_data["values"][mb_inds] + torch.clamp(
                        newvalue - rollout_data["values"][mb_inds],
                        -args.clip_coef,
                        args.clip_coef,
                    )
                    v_loss_clipped = (v_clipped - returns[mb_inds]) ** 2
                    v_loss_max = torch.max(v_loss_unclipped, v_loss_clipped)
                    v_loss = 0.5 * v_loss_max.mean()
                else:
                    v_loss = 0.5 * ((newvalue - returns[mb_inds]) ** 2).mean()
                    # print('v_loss: {}'.format(v_loss))

                entropy_loss = updated_rollout_data["entropy"].mean()
                loss = (
                    pg_loss
                    - args.ent_coef * entropy_loss
                    + v_loss * args.vf_coef
                )

                if args.do_training:
                    optimizer.zero_grad()
                    loss.backward()
                    nn.utils.clip_grad_norm_(
                        td_module.parameters(), args.max_grad_norm
                    )
                    optimizer.step()


            if args.target_kl is not None:
                if approx_kl > args.target_kl:
                    break
        print('update nr: {}'.format(update))
        # TRY NOT TO MODIFY: record rewards for plotting purposes
        writer.add_scalar(
            "charts/learning_rate", optimizer.param_groups[0]["lr"], global_step
        )
        writer.add_scalar("losses/value_loss", v_loss.item(), global_step)
        writer.add_scalar("losses/policy_loss", pg_loss.item(), global_step)
        writer.add_scalar("losses/entropy", entropy_loss.item(), global_step)
        writer.add_scalar("losses/old_approx_kl", old_approx_kl.item(), global_step)
        writer.add_scalar("losses/approx_kl", approx_kl.item(), global_step)
        writer.add_scalar("losses/clipfrac", np.mean(clipfracs), global_step)
        print("SPS:", int(global_step / (time.time() - start_time)))
        writer.add_scalar("charts/SPS", int(global_step / (time.time() - start_time)), global_step)

    writer.close()
