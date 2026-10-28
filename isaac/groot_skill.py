"""GR00T arm skill for the Isaac Sim port (starter code, NOT TESTED).

NVIDIA Isaac GR00T N1.x is a vision-language-action model: given camera images,
the robot's joint state and a short instruction ("pick up the red box and put it
on the low shelf, left"), it outputs the next chunk of joint actions.

Nemotron stays the planner (which box goes where, recovery). GR00T replaces the
scripted PickPlaceController for each pick+place step.

Setup (Linux, NVIDIA GPU), from https://github.com/NVIDIA/Isaac-GR00T:
  1. Fine-tune or pick a GR00T checkpoint for the Franka (or your arm).
  2. Start the policy server, e.g.:
       python scripts/inference_service.py --server --model_path <checkpoint> \\
           --embodiment_tag new_embodiment --data_config <your_franka_config>
  3. Run: ./python.sh isaac/shelfmind_isaac.py --groot

The observation keys below must match the data config you trained with; adjust
them for your setup.
"""

import numpy as np

STEPS_PER_CHUNK = 16
MAX_CHUNKS = 40


class GrootSkill:
    def __init__(self, franka, fallback, host="localhost", port=5555):
        self.franka = franka
        self.fallback = fallback
        self.client = None
        try:
            from gr00t.eval.robot import RobotInferenceClient
            self.client = RobotInferenceClient(host=host, port=port)
            print("[GR00T] Connected to the policy server at %s:%d" % (host, port))
        except Exception as e:  # no gr00t package or no server: use the scripted skill
            print("[GR00T] Not available (%s); using the scripted pick-and-place." % e)

    def observation(self, instruction):
        joints = self.franka.get_joint_positions()
        return {
            "video.ego_view": self.camera_image()[None],          # (1, H, W, 3) uint8
            "state.arm": joints[None, :7].astype(np.float32),
            "state.gripper": joints[None, 7:].astype(np.float32),
            "annotation.human.action.task_description": [instruction],
        }

    def camera_image(self):
        # TODO: render from a camera prim, e.g. isaacsim.sensors.camera.Camera(...).get_rgba()[..., :3]
        return np.zeros((224, 224, 3), dtype=np.uint8)

    def run(self, world, box, target):
        if self.client is None:
            return self.fallback.run(world, box, target)
        name = box.name.replace("_", " ")
        instruction = "pick up the %s and place it at x %.2f y %.2f" % (name, target[0], target[1])
        from isaacsim.core.utils.types import ArticulationAction
        for _ in range(MAX_CHUNKS):
            action = self.client.get_action(self.observation(instruction))
            arm = np.asarray(action["action.arm"])          # (T, 7)
            grip = np.asarray(action["action.gripper"])     # (T, 1) or (T, 2)
            for t in range(min(STEPS_PER_CHUNK, len(arm))):
                g = np.repeat(grip[t].reshape(-1)[:1], 2)
                self.franka.apply_action(ArticulationAction(joint_positions=np.concatenate([arm[t], g])))
                world.step(render=True)
            pos, _ = box.get_world_pose()
            if np.linalg.norm(pos[:2] - target[:2]) < 0.03 and abs(pos[2] - target[2]) < 0.05:
                return
