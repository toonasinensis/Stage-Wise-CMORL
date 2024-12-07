# env
from tasks import task_dict

# algorithm
from algos import algo_dict

# utils
from utils import backupFiles, setSeed, cprint
from utils.wrapper import EnvWrapper
from utils.slackbot import Slackbot
from utils.logger import Logger

# base
from ruamel.yaml import YAML
from copy import deepcopy
import numpy as np
import argparse
import torch
import wandb
import time
import glob
import csv
def getParser():
    parser = argparse.ArgumentParser()
    # common
    parser.add_argument('--wandb', action='store_true', help='use wandb?')
    parser.add_argument('--slack', action='store_true', help='use slack?')
    parser.add_argument('--test', action='store_true', help='test or train?')
    parser.add_argument('--device_type', type=str, default='gpu', help='gpu or cpu.')
    parser.add_argument('--gpu_idx', type=int, default=0, help='GPU index.')
    parser.add_argument('--model_num', type=int, default=0, help='num model.')
    parser.add_argument('--save_freq', type=int, default=int(1e7), help='# of time steps for save.')
    parser.add_argument('--wandb_freq', type=int, default=int(5e4), help='# of time steps for wandb logging.')
    parser.add_argument('--slack_freq', type=int, default=int(2.5e6), help='# of time steps for slack message.')
    parser.add_argument('--seed', type=int, default=1, help='seed number.')
    parser.add_argument('--task_cfg_path', type=str, help='cfg.yaml file location for task.')
    parser.add_argument('--algo_cfg_path', type=str, help='cfg.yaml file location for algorithm.')
    parser.add_argument('--project_name', type=str, default="Stage-Wise-CMORL", help='wandb project name.')
    parser.add_argument('--render',  action='store_true', help='rendering?')
    parser.add_argument('--comment', type=str, default=None, help='wandb comment saved in run name.')
    return parser

def train(args, task_cfg, algo_cfg):
    # set seed
    setSeed(args.seed)

    # backup configurations
    backupFiles(f"{args.save_dir}/backup", task_cfg['backup_files'], algo_cfg['backup_files'])

    # create environments
    env_fn = lambda: task_dict[task_cfg['name']](
        cfg=task_cfg, rl_device=args.device_name, sim_device=args.device_name, 
        graphics_device_id=0, headless=(not args.render), 
        virtual_screen_capture=False, force_render=args.render
    )
    vec_env = EnvWrapper(env_fn)

    # set arguments
    args.device = vec_env.unwrapped.rl_device
    args.n_envs = vec_env.unwrapped.num_envs
    args.max_episode_len = vec_env.unwrapped.max_episode_length
    args.num_stages = vec_env.unwrapped.num_stages
    args.obs_dim = vec_env.unwrapped.num_obs
    args.state_dim = vec_env.unwrapped.num_states - args.num_stages
    args.action_dim = vec_env.unwrapped.num_acts
    args.reward_dim = vec_env.unwrapped.num_rewards
    args.cost_dim = vec_env.unwrapped.num_costs
    args.action_bound_min = -np.ones(args.action_dim)
    args.action_bound_max = np.ones(args.action_dim)
    args.n_steps = algo_cfg['n_steps']
    args.n_total_steps = task_cfg['n_total_steps']
    args.reward_names = task_cfg["env"]["reward_names"]
    args.cost_names = task_cfg["env"]["cost_names"]
    assert len(args.reward_names) == args.reward_dim
    assert len(args.cost_names) == args.cost_dim
    args.history_len = vec_env.unwrapped.history_len
    args.obs_sym_mat = vec_env.unwrapped.obs_sym_mat
    args.state_sym_mat = vec_env.unwrapped.state_sym_mat
    args.joint_sym_mat = vec_env.unwrapped.joint_sym_mat

    # declare agent
    agent_args = deepcopy(args)
    for key in algo_cfg.keys():
        agent_args.__dict__[key] = algo_cfg[key]
    agent = algo_dict[args.algo_name.lower()](agent_args)
    initial_step = agent.load(args.model_num)

    # declare teacher
    teacher_args = deepcopy(args)
    teacher_args.seed = algo_cfg['teacher']['seed']
    teacher_args.algo_name = algo_cfg['teacher']['algo_name']
    teacher_args.model_num = algo_cfg['teacher']['model_num']
    teacher_args.name = f"{(teacher_args.task_name.lower())}_{(teacher_args.algo_name.lower())}"
    teacher_args.save_dir = f"results/{teacher_args.name}/seed_{teacher_args.seed}"
    backup_file_name = glob.glob(f"{teacher_args.save_dir}/backup/algo/*.yaml")[0]
    with open(backup_file_name, 'r') as f:
        teacher_algo_cfg = YAML().load(f)
    for key in teacher_algo_cfg.keys():
        teacher_args.__dict__[key] = teacher_algo_cfg[key]
    teacher_args.obs_dim = vec_env.unwrapped.raw_obs_dim * teacher_args.history_len
    teacher_agent = algo_dict[teacher_args.algo_name.lower()](teacher_args)
    assert teacher_agent.load(teacher_args.model_num) != 0
    # copy teacher's obs_rms to student
    agent.copyObsRMS(teacher_agent.obs_rms)

    # wandb
    if args.wandb:
        wandb.init(project=args.project_name, config=args)
        if args.comment is not None:
            wandb.run.name = f"{args.name}/{args.comment}"
        else:
            wandb.run.name = f"{args.name}"

    # slackbot
    if args.slack:
        slackbot = Slackbot()

    # logger
    log_name_list = deepcopy(agent_args.logging['task_indep'])
    for log_name in agent_args.logging['reward_dep']:
        log_name_list += [f"{log_name}_{reward_name}" for reward_name in args.reward_names]
    for log_name in agent_args.logging['cost_dep']:
        log_name_list += [f"{log_name}_{cost_name}" for cost_name in args.cost_names]
    logger = Logger(log_name_list, f"{args.save_dir}/logs")

    # set train parameters
    reward_sums_tensor = torch.zeros((args.n_envs, args.reward_dim), device=args.device, requires_grad=False, dtype=torch.float32)
    cost_sums_tensor = torch.zeros((args.n_envs, args.cost_dim), device=args.device, requires_grad=False, dtype=torch.float32)
    fail_sums_tensor = torch.zeros(args.n_envs, device=args.device, requires_grad=False, dtype=torch.float32)
    env_cnts_tensor = torch.zeros(args.n_envs, device=args.device, requires_grad=False, dtype=torch.int)
    total_step = initial_step
    wandb_step = initial_step
    slack_step = initial_step
    save_step = initial_step

    # initialize environments
    with torch.no_grad():
        obs_tensor, states_tensor = vec_env.reset()
        stages_tensor = states_tensor[:, -args.num_stages:]
        states_tensor = states_tensor[:, :-args.num_stages]

    # start training
    for _ in range(int(initial_step/args.n_steps), int(args.n_total_steps/args.n_steps)):
        start_time = time.time()

        for _ in range(int(args.n_steps/args.n_envs)):
            env_cnts_tensor += 1
            total_step += args.n_envs

            # ======= collect trajectories & training ======= #
            with torch.no_grad():
                teacher_actions_tensor = teacher_agent.getAction(obs_tensor[:, -teacher_args.obs_dim:], states_tensor, stages_tensor, True)
                student_actions_tensor = agent.getAction(obs_tensor, True)
                agent.step(obs_tensor.clone(), teacher_actions_tensor.clone())

                if ((total_step//int(1e5)) % 2 == 0) or (total_step < 1e7):
                    actions_tensor = teacher_actions_tensor
                else:
                    actions_tensor = student_actions_tensor
                obs_tensor, states_tensor, rewards_tensor, dones, infos = vec_env.step(actions_tensor)
                stages_tensor = states_tensor[:, -args.num_stages:]
                states_tensor = states_tensor[:, :-args.num_stages]
                costs_tensor = infos['costs']
                fails_tensor = infos['fails']

            reward_sums_tensor += rewards_tensor
            cost_sums_tensor += costs_tensor
            fail_sums_tensor += fails_tensor
            # =============================================== #

            # wandb logging
            if total_step - wandb_step >= args.wandb_freq and args.wandb:
                wandb_step += args.wandb_freq

                # write log using logger
                env_cnts = env_cnts_tensor.detach().cpu().numpy()
                reward_sums = reward_sums_tensor.detach().cpu().numpy()
                cost_sums = cost_sums_tensor.detach().cpu().numpy()
                fail_sums = fail_sums_tensor.detach().cpu().numpy()
                if 'eplen' in logger.log_name_list: 
                    logger.writes('eplen', np.stack([env_cnts, env_cnts]).T.tolist())
                if 'fail' in logger.log_name_list: 
                    logger.writes('fail', np.stack([env_cnts, fail_sums]).T.tolist())
                for reward_idx in range(args.reward_dim):
                    log_name = f'reward_sum_{args.reward_names[reward_idx]}'
                    if log_name in logger.log_name_list:
                        logger.writes(log_name, np.stack([env_cnts, reward_sums[:, reward_idx]]).T.tolist())
                for cost_idx in range(args.cost_dim):
                    log_name = f'cost_sum_{args.cost_names[cost_idx]}'
                    if log_name in logger.log_name_list: 
                        logger.writes(log_name, np.stack([env_cnts, cost_sums[:, cost_idx]]).T.tolist())
                reward_sums_tensor[:] = 0
                cost_sums_tensor[:] = 0
                fail_sums_tensor[:] = 0
                env_cnts_tensor[:] = 0

                # write log using wandb
                log_data = {"step": total_step}
                print_len = args.n_envs
                print_len2 = int(args.wandb_freq/args.n_steps)
                for reward_idx, reward_name in enumerate(args.reward_names):
                    for log_name in agent_args.logging['reward_dep']:
                        log_data[f'{log_name}/{reward_name}'] = logger.get_avg(f'{log_name}_{reward_name}', print_len if 'sum' in log_name else print_len2)
                for cost_idx, cost_name in enumerate(args.cost_names):
                    for log_name in agent_args.logging['cost_dep']:
                        log_data[f'{log_name}/{cost_name}'] = logger.get_avg(f'{log_name}_{cost_name}', print_len if 'sum' in log_name else print_len2)
                for log_name in agent_args.logging['task_indep']:
                    log_data[f"metric/{log_name}"] = logger.get_avg(log_name, print_len if log_name in ['eplen', 'fail'] else print_len2)
                wandb.log(log_data)
                print(log_data)
                print('\n')
                print('\n')
                print('\n')

            # send slack message
            if total_step - slack_step >= args.slack_freq and args.slack:
                slackbot.sendMsg(f"{args.project_name}\nname: {wandb.run.name}\nsteps: {total_step}\nlog: {log_data}")
                slack_step += args.slack_freq

            # save
            if total_step - save_step >= args.save_freq:
                save_step += args.save_freq
                agent.save(total_step)
                logger.save()

        # train
        if agent.readyToTrain():
            train_results = agent.train()
            for log_name in train_results.keys():
                if log_name in agent_args.logging['task_indep']:
                    logger.write(log_name, [args.n_steps, train_results[log_name]])
                elif log_name in agent_args.logging['reward_dep']:
                    for reward_idx, reward_name in enumerate(args.reward_names):
                        logger.write(f"{log_name}_{reward_name}", [args.n_steps, train_results[log_name][reward_idx]])
                elif log_name in agent_args.logging['cost_dep']:
                    for cost_idx, cost_name in enumerate(args.cost_names):
                        logger.write(f"{log_name}_{cost_name}", [args.n_steps, train_results[log_name][cost_idx]])

        # calculate FPS
        end_time = time.time()
        fps = args.n_steps/(end_time - start_time)
        if 'fps' in logger.log_name_list:
            logger.write('fps', [args.n_steps, fps])

    # final save
    agent.save(total_step)
    logger.save()

    # terminate
    vec_env.close()


def test(args, task_cfg, algo_cfg):
    # create environments
    task_cfg['env']['num_envs'] = 1
    if 'randomize' in task_cfg['env']:
        task_cfg['env']['randomize']['is_randomized'] = False
    env_fn = lambda: task_dict[task_cfg['name']](
        cfg=task_cfg, rl_device=args.device_name, sim_device=args.device_name, 
        graphics_device_id=0, headless=(not args.render), 
        virtual_screen_capture=False, force_render=args.render
    )
    vec_env = EnvWrapper(env_fn)
    args.device = vec_env.unwrapped.rl_device
    args.n_envs = vec_env.unwrapped.num_envs
    args.max_episode_len = vec_env.unwrapped.max_episode_length
    args.num_stages = vec_env.unwrapped.num_stages
    args.obs_dim = vec_env.unwrapped.num_obs
    args.state_dim = vec_env.unwrapped.num_states - args.num_stages
    args.action_dim = vec_env.unwrapped.num_acts
    args.reward_dim = vec_env.unwrapped.num_rewards
    args.cost_dim = vec_env.unwrapped.num_costs
    args.action_bound_min = -np.ones(args.action_dim)
    args.action_bound_max = np.ones(args.action_dim)
    args.n_steps = algo_cfg['n_steps']
    args.n_total_steps = task_cfg['n_total_steps']
    args.reward_names = task_cfg["env"]["reward_names"]
    args.cost_names = task_cfg["env"]["cost_names"]
    assert len(args.reward_names) == args.reward_dim
    assert len(args.cost_names) == args.cost_dim
    args.history_len = vec_env.unwrapped.history_len
    args.obs_sym_mat = vec_env.unwrapped.obs_sym_mat
    args.state_sym_mat = vec_env.unwrapped.state_sym_mat
    args.joint_sym_mat = vec_env.unwrapped.joint_sym_mat

    # declare agent
    agent_args = deepcopy(args)
    for key in algo_cfg.keys():
        agent_args.__dict__[key] = algo_cfg[key]
    agent = algo_dict[args.algo_name.lower()](agent_args)
    agent.load(args.model_num)
    agent.save_actor()
    with torch.no_grad():
        obs_tensor, states_tensor = vec_env.reset(is_uniform_rollout=False)
    count=0
    mean = np.loadtxt('mean.csv', delimiter=',')
    var = np.loadtxt('var.csv',delimiter=',')

    
    
    # start rollouts
    policy_exp = torch.jit.load("model.pt")
    policy_exp.eval()
    policy_exp.to("cpu")
    for _ in range(100):
        reward_sums_tensor = torch.zeros((args.n_envs, args.reward_dim), device=args.device, requires_grad=False, dtype=torch.float32)
        cost_sums_tensor = torch.zeros((args.n_envs, args.cost_dim), device=args.device, requires_grad=False, dtype=torch.float32)
        start_time = time.time()
        filename = "tensor_output.csv"

        # 打开文件以便写入
        with open(filename, mode='w', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            writer.writerow(["orint", "dof_p", "dof_v","v","p","omgea"])  # 替换为你的列名

            for step_idx in range(args.max_episode_len):
                with torch.no_grad():
                    count+=0.02

                    reshaped_obs = obs_tensor.view(-1, 420)
                    # print(reshaped_obs[:,-42:])
                    cur_mean = torch.tensor(mean).to(agent.device)
                    cur_var = torch.tensor(var).to(agent.device)
                    reshaped_mean = cur_mean.view(1, -1).tile(1,10)
                    reshaped_var = cur_var.view(1, -1).tile(1, 10)
                    norm_obs = (reshaped_obs - reshaped_mean)/torch.sqrt(reshaped_var + 1e-8)
                    norm_obs=norm_obs.to(torch.float32)
                    # print(norm_obs[-42:])
                    # print("reshaped_obs",reshaped_obs[:,-42:-27])
                    actions_tensor=policy_exp(norm_obs.to("cpu"))
                    # print(actions_tensor)
                    obs_tensor, states_tensor, rewards_tensor, dones_tensor, infos = vec_env.step(actions_tensor.to("cuda"))
                    print(infos['next_states'].size())
                    print("print(states_tensor.size())",infos['next_states'])

                    dataset = torch.cat([reshaped_obs[:,-42:-42+24+3], infos['next_states'][:,:9]  ],dim = -1)
                    numpy_data = dataset.to("cpu").numpy()
                    writer.writerows(numpy_data)
                    reward_sums_tensor += rewards_tensor
                    cost_sums_tensor += infos['costs']
                    if infos['dones'][0]:
                        break
                    elapsed_time = time.time() - start_time
                    if elapsed_time < (step_idx + 1)*vec_env.unwrapped.control_dt:
                        time.sleep((step_idx + 1)*vec_env.unwrapped.control_dt - elapsed_time)

            print(time.time() - start_time)
            print(reward_sums_tensor[0].cpu().numpy())
            print(cost_sums_tensor[0].cpu().numpy())
        # =============================================== #


if __name__ == "__main__":
    parser = getParser()
    args = parser.parse_args()

    # ==== processing args ==== #
    # load configuration file
    with open(args.task_cfg_path, 'r') as f:
        task_cfg = YAML().load(f)
    args.task_name = task_cfg['name']
    with open(args.algo_cfg_path, 'r') as f:
        algo_cfg = YAML().load(f)
    args.algo_name = algo_cfg['name']
    args.name = f"{(args.task_name.lower())}_{(args.algo_name.lower())}"
    # save_dir
    args.save_dir = f"results/{args.name}/seed_{args.seed}"
    # device
    if torch.cuda.is_available() and args.device_type == 'gpu':
        device_name = f'cuda:{args.gpu_idx}'
        cprint('[torch] cuda is used.', bold=True, color='cyan')
    else:
        device_name = 'cpu'
        cprint('[torch] cpu is used.', bold=True, color='cyan')
    args.device_name = device_name
    # ========================= #

    if args.test:
        test(args, task_cfg, algo_cfg)
    else:
        train(args, task_cfg, algo_cfg)
