import serial
import numpy as np
import cv2
import time
import matplotlib.pyplot as plt
import asyncio

import gymnasium as gym
from stable_baselines3 import PPO
from stable_baselines3.common.env_checker import check_env
from imutils.video import VideoStream

from module.cam_module import BallTracker
from module.fsr_slider_module import FSRSerialReader
from module.imu_module import BLEIMUHandler
from module.dxl_module import DXLHandler

class HandEnv(gym.Env):
    DXL_MINIMUM_POSITION_VALUE = 1500
    DXL_MAXIMUM_POSITION_VALUE = 2500

    def __init__(self, render_mode='human'):
        super(HandEnv, self).__init__()

        self.render_mode = render_mode
        self.action_space = gym.spaces.Box(low=-1.0, high=1.0, shape=(7,), dtype=np.float32)
        # Preprocessing (grayscale conversion or downscaling) could be applied to speed up training
        self.observation_space = gym.spaces.Box(low=-4.0, high=4.0, shape=(10,), dtype=np.float32)

        self.dxl_ids = [10, 11, 12, 20, 21, 22, 30, 31, 32] # define idx of motors

        # initial sensors
        # start camera
        self.vs = cv2.VideoCapture(0)
        self.cam = BallTracker(buffer_size=64, height_threshold=300, alpha=0.2)
        # start fsr & slider
        self.fsr = FSRSerialReader(port='COM5', baudrate=115200, threshold=50)
        self.fsr.start_collection()
        # time.sleep(1)
        # start imu
        self.imu = BLEIMUHandler(target_device = "D1:9D:96:C7:9D:E4")
        debug_code = self.imu.start_imu()
        if debug_code < 0:
            raise Exception("[IMU] Failed to open IMU")

        self.dxl = DXLHandler(port='COM4', baudrate=1000000)
        self.dxl.start_dxl()

        self._ij = 0

        self.lifting_rewards = []
        self.rotation_rewards = []
        self.accumulated_rewards = []

################################################################################################
# main function
################################################################################################

    def step(self, action):
        truncated = False

        # move slider using 6th value in action
        self.move_slider(action[6])
        # move dclaw using 0-5th value in action
        idx = [11, 12, 21, 22, 31, 32]
        positions = self.map_array(action[:6], [-1, 1], [self.DXL_MINIMUM_POSITION_VALUE, self.DXL_MAXIMUM_POSITION_VALUE])
        if self.dxl.move_to_position(idx, positions) <= 0:
            truncated = True

        # get observation
        obs_pos = self.dxl.read_positions(idx)
        force_D0, force_D1, force_D2 = self.fsr.get_fsr()
        observation = [*obs_pos, action[6], force_D0, force_D1, force_D2]
        observation = np.array(observation, dtype=np.float32)

        # monitor motor temperature
        if np.any(self.dxl.read_temperature() > 70):
            return observation, 0, True, False, {}

        # define different curriculum
        if self._ij > 999999:
            rot_weight = 1
            lift_weight = 1
        else:
            rot_weight = 1
            lift_weight = 0

        self.camera_update()

        # get reward
        lifting_reward, _ = self.cam.get_rewards()
        x_velocity, y_velocity, z_velocity = self.imu.updateIMUData()
        rotation_reward = np.sqrt(x_velocity**2 + y_velocity**2 + z_velocity**2)
        reward = lifting_reward * lift_weight + rotation_reward * rot_weight

        self.lifting_rewards.append(lifting_reward)
        self.rotation_rewards.append(rotation_reward)
        self.accumulated_rewards.append(reward)
        self._ij += 1 

        # define terminate and truncate condition
        done = self.check_done()
        info = {}
        # truncated = self.check_episode()

        return observation, reward, done, truncated, info

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)  # Pass the seed to the parent class if necessary

        # self.move_actuators(idx=self.dxl_ids, action=init_pos)  # Move actuators to initial positions

        self.move_slider(1)
        dxl_code = self.dxl.move_to_position(self.dxl.DXL_IDs, self.dxl.DXL_INIT_POS)
        if dxl_code <= 0:
            raise Exception("[DXL] DXL is stuck")

        # retrieve force values
        force_D0, force_D1, force_D2 = self.fsr.get_fsr()

        # read current motor position
        idx = [11, 12, 21, 22, 31, 32]
        obs_pos = self.dxl.read_positions(idx)
        if np.any(np.array(obs_pos) == -1):
            raise Exception("[DXL] Can't read position")
        
        observation = [*obs_pos, 1, force_D0, force_D1, force_D2]
        observation = np.array(observation, dtype=np.float32)

        return observation, {}

    def render(self, mode='human'):
        self.camera_update()
        frame = self.cam.get_frame()

        cv2.imshow('Camera Output', frame)
        cv2.waitKey(1)

    def close(self):
        if self.dxl is not None:
            self.dxl.stop_dxl()
        if self.fsr is not None:
            self.fsr.stop_collection()
        if self.imu is not None:
            self.imu.stop_imu()
        # self.plot_ball_positions()
        self.plot_accumulated_rewards()
        self.vs.release()

################################################################################################
# other function
################################################################################################

    # move slider
    def move_slider(self, action):
        slider_position = np.interp(action, [-1.0, 1.0], [75, 145])
        slider_position = str(int(round(slider_position)))
        # print("slider_position: ", slider_position)
        respond = self.fsr.send_slider_position(slider_position)
        time.sleep(1)
        # print(respond)

    def camera_update(self):
        _, frame = self.vs.read()
        self.cam.track_ball(frame)  # Process the frame with the tracker
    
    # termination func
    def check_done(self):
        if self._ij >= 1000:
            return True
        return False
    
    # truncation func
    def check_episode(self):
        if self._ij % 10 == 0:
            return True
        return False

    def map_array(self, arr, a, b):
            # Convert arr to a NumPy array for mathematical operations
            arr = np.array(arr, dtype=np.float64)
            
            # Perform linear mapping calculation
            mapped_arr = (arr - a[0]) * (b[1] - b[0]) / (a[1] - a[0]) + b[0]
            
            # Return a single float if the input is a single value
            if np.isscalar(arr) or isinstance(arr, (int, float)):
                return float(mapped_arr)  # Ensure the return value is a pure Python number
            else:
                return mapped_arr.tolist()  # Convert back to a Python list

    def plot_accumulated_rewards(self):
        plt.figure(figsize=(10, 6))
        plt.plot(range(len(self.lifting_rewards)), self.lifting_rewards, label='Lifting Reward', color='blue')
        plt.plot(range(len(self.rotation_rewards)), self.rotation_rewards, label='Rotation Reward', color='orange')
        plt.plot(range(len(self.accumulated_rewards)), self.accumulated_rewards, label='Accumulated Reward', color='green')
        plt.title('Accumulated Reward over Time')
        plt.xlabel('Timesteps')
        plt.ylabel('Accumulated Reward')
        plt.grid(True)
        plt.legend()
        plt.tight_layout()
        plt.show()


################################################################################################
# 
################################################################################################


if __name__ == "__main__":
    env = HandEnv(render_mode="human")

    try:
        check_env(env)  # Check if the environment is valid

        # # Define the model
        # model = PPO('MlpPolicy', env, verbose=1)

        # # Train the model
        # model.learn(total_timesteps=1000)

        # # Save the model
        # model.save("ppo_hand_env")

        # # Load the model
        # model = PPO.load("ppo_hand_env")

        # obs, _ = env.reset()
        done = False
        
        while not done:
            # action, _states = model.predict(obs)  # Let the model decide the action
            action = env.action_space.sample()
            print("random action:", action)
            obs, reward, done, truncated, _ = env.step(action)
            if truncated:
                env.reset()
            # print(obs)
            # env.render()  # Render the environment (optional)
    except Exception as e:
        print(e)
    except KeyboardInterrupt:
        print("Interrupt")
    # Close the environment
    env.close()