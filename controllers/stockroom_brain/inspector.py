"""Humanoid inspector: a small walking humanoid robot (about 0.95 m) that patrols
the stockroom and reports problems to the brain.

The humanoid has no controller of its own. The supervisor moves it every time
step: it walks the two side lanes (legs and arms swing, the body bobs), stops at
three inspection points per lane, turns its head towards the shelves and asks
for an inspection. When it hears a crash it hurries to the noisy zone.
"""

import math

LANE_Y = 0.515           # it walks at y = +/-0.515, between the shelves and the frame posts
WALK_SPEED = 0.35        # m/s
RUSH_SPEED = 0.6         # m/s after hearing a crash
TURN_SPEED = 4.0         # rad/s
LOOK_S = 1.2             # how long it looks before inspecting
EYE_HEIGHT = 0.88        # camera in the visor

# zone name -> (x where it stops, (x range it can inspect))
ZONES = {
    "table": (-0.5, (-0.9, -0.15)),
    "floor gap": (0.0, (-0.25, 0.22)),
    "shelves": (0.55, (0.15, 0.95)),
}
# (x, lane: -1 near side / +1 far side, zone to inspect or None)
ROUTE = [(-1.2, -1, None), (-0.5, -1, "table"), (0.0, -1, "floor gap"), (0.55, -1, "shelves"),
         (1.2, -1, None), (1.2, 1, None), (0.55, 1, "shelves"), (0.0, 1, "floor gap"),
         (-0.5, 1, "table"), (-1.2, 1, None)]


def wrap_angle(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


class Inspector:
    def __init__(self, supervisor):
        def field(name, f):
            return supervisor.getFromDef(name).getField(f)
        self.translation = field("INSPECTOR", "translation")
        self.rotation = field("INSPECTOR", "rotation")
        self.body = field("INSPECTOR_BODY", "translation")
        self.head = field("INSPECTOR_HEAD", "rotation")
        self.legs = (field("INSPECTOR_LEG_L", "rotation"), field("INSPECTOR_LEG_R", "rotation"))
        self.arms = (field("INSPECTOR_ARM_L", "rotation"), field("INSPECTOR_ARM_R", "rotation"))
        self.body_z = self.body.getSFVec3f()[2]
        self.x, self.y, _ = self.translation.getSFVec3f()
        self.yaw = 0.0
        self.head_yaw = 0.0
        self.phase = 0.0
        self.on = True
        self.wp = 1
        self.mode = "walk"        # walk | look | wait (for the inspection result)
        self.timer = 0.0
        self.zone = None
        self.alert = None         # zone to rush to after a crash
        self.request = None       # zone waiting to be inspected by the controller
        self.lap = 1
        self.reports = 0
        self.last = "starting the first patrol"

    # ------------------------------------------------------------- route
    def lane(self):
        return 1 if self.y > 0 else -1

    def target(self):
        if self.alert:
            return ZONES[self.alert][0], self.lane() * LANE_Y, self.alert
        x, lane, zone = ROUTE[self.wp]
        return x, lane * LANE_Y, zone

    def next(self):
        """Go on with the patrol after an inspection."""
        if self.alert:
            self.alert = None
            # continue from the next route point on this lane
            here = [i for i, (x, lane, _) in enumerate(ROUTE) if lane == self.lane()]
            nearest = min(here, key=lambda i: abs(ROUTE[i][0] - self.x))
            self.wp = (nearest + 1) % len(ROUTE)
        else:
            self.wp = (self.wp + 1) % len(ROUTE)
            if self.wp == 0:
                self.lap += 1
        self.mode = "walk"
        self.zone = None

    def hear(self, x):
        """A crash near x: hurry to the zone it came from."""
        if not self.on:
            return
        self.alert = min(ZONES, key=lambda z: abs(ZONES[z][0] - x))
        if self.mode == "look":
            self.mode = "walk"
        self.last = "heard a crash near the %s!" % self.alert

    def eye(self):
        """Camera position (in front of the visor) and the point it is looking at."""
        if self.zone:
            look = (ZONES[self.zone][0], 0.0, 0.3)
            a = math.atan2(look[1] - self.y, look[0] - self.x)
        else:
            a = self.yaw + self.head_yaw
            look = (self.x + math.cos(a), self.y + math.sin(a), 0.5)
        pos = (self.x + 0.12 * math.cos(a), self.y + 0.12 * math.sin(a), EYE_HEIGHT)
        return pos, look

    # ---------------------------------------------------------- stepping
    def update(self, dt, can_inspect):
        walking = False
        if not self.on:
            self.head_yaw *= 0.9
        elif self.mode == "look":
            zx = ZONES[self.zone][0]
            want = wrap_angle(math.atan2(-self.y, zx - self.x) - self.yaw)
            want = max(-1.5, min(1.5, want))
            self.head_yaw += max(-TURN_SPEED * dt, min(TURN_SPEED * dt, want - self.head_yaw))
            self.timer -= dt
            if self.timer <= 0:
                if can_inspect:
                    self.request = self.zone
                    self.mode = "wait"
                else:
                    self.next()
        elif self.mode == "walk":
            self.head_yaw *= 0.85
            tx, ty, zone = self.target()
            dx, dy = tx - self.x, ty - self.y
            dist = math.hypot(dx, dy)
            if dist < 0.01:
                if zone:
                    self.mode, self.zone, self.timer = "look", zone, LOOK_S
                else:
                    self.next()
            else:
                turn = wrap_angle(math.atan2(dy, dx) - self.yaw)
                self.yaw += max(-TURN_SPEED * dt, min(TURN_SPEED * dt, turn))
                if abs(turn) < 0.5:
                    step = min(dist, (RUSH_SPEED if self.alert else WALK_SPEED) * dt)
                    self.x += dx / dist * step
                    self.y += dy / dist * step
                walking = True
        if walking:
            self.phase += dt * (11 if self.alert else 8)
        else:
            self.phase *= 0.8  # settle the arms and legs
        swing = 0.4 * math.sin(self.phase) if walking else 0.0
        bob = 0.012 * abs(math.sin(self.phase)) if walking else 0.0
        self.translation.setSFVec3f([self.x, self.y, 0.0])
        self.rotation.setSFRotation([0, 0, 1, self.yaw])
        self.body.setSFVec3f([0, 0, self.body_z + bob])
        self.head.setSFRotation([0, 0, 1, self.head_yaw])
        self.legs[0].setSFRotation([0, 1, 0, swing])
        self.legs[1].setSFRotation([0, 1, 0, -swing])
        self.arms[0].setSFRotation([0, 1, 0, -0.8 * swing])
        self.arms[1].setSFRotation([0, 1, 0, 0.8 * swing])

    def describe(self):
        if not self.on:
            return "standing by (press H to start the patrol)"
        if self.mode == "walk":
            return ("hurrying to the %s" % self.alert) if self.alert else "patrolling (lap %d)" % self.lap
        return "inspecting the %s" % self.zone
