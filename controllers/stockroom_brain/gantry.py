"""Gantry robots. One interface for two kinds:

- MotorGantry: the controller's own robot, driven by its LinearMotors.
- JointGantry: a second gantry (no controller of its own), moved by the
  supervisor with setJointPosition at the same speed limit.
"""

PAD0 = 1.025      # pad bottom height when z = 0
SPEED = 0.6       # m/s
AXES = "xyz"


class Gantry:
    def __init__(self, name, limits, home_x):
        self.name = name
        self.limits = limits          # {"x": (lo, hi), ...}
        self.home_x = home_x
        self.held = None              # box name
        self.drop_event = None        # box that just slipped
        self.waiting = False          # blocked by the other robot
        self.claim = None             # (x_lo, x_hi) of rail it may use
        self.speed_scale = 1.0
        self.vacuum = None            # VacuumGripper device, if any

    def pad(self):
        return self.pos("x"), self.pos("y"), PAD0 - self.pos("z")

    def reachable(self, x, y, z):
        lim = self.limits
        return (lim["x"][0] <= x <= lim["x"][1] and lim["y"][0] <= y <= lim["y"][1]
                and lim["z"][0] <= z <= lim["z"][1])

    def update(self, dt):
        pass


class MotorGantry(Gantry):
    def __init__(self, robot, name, ts, home_x=0.0):
        self.motors, self.sensors = {}, {}
        limits = {}
        for a in AXES:
            m = robot.getDevice(a + "_motor")
            m.setVelocity(SPEED)
            s = robot.getDevice(a + "_sensor")
            s.enable(ts)
            self.motors[a], self.sensors[a] = m, s
            limits[a] = (m.getMinPosition(), m.getMaxPosition())
        super().__init__(name, limits, home_x)
        for a in AXES:
            self.motors[a].setPosition(0.0 if a != "x" else home_x)

    def pos(self, a):
        return self.sensors[a].getValue()

    def command(self, a, v):
        self.motors[a].setPosition(v)

    def stop(self, a):
        self.motors[a].setPosition(self.pos(a))

    def set_speed(self, scale):
        self.speed_scale = scale
        for m in self.motors.values():
            m.setVelocity(SPEED * scale)


class JointGantry(Gantry):
    def __init__(self, supervisor, name, prefix, limits, home_x):
        super().__init__(name, limits, home_x)
        self.joints, self.p, self.t = {}, {}, {}
        for a in AXES:
            node = supervisor.getFromDef("%s_%s_JOINT" % (prefix, a.upper()))
            self.joints[a] = node
            params = node.getField("jointParameters").getSFNode()
            self.p[a] = self.t[a] = params.getField("position").getSFFloat()

    def pos(self, a):
        return self.p[a]

    def command(self, a, v):
        lo, hi = self.limits[a]
        self.t[a] = min(max(v, lo), hi)

    def stop(self, a):
        self.t[a] = self.p[a]

    def set_speed(self, scale):
        self.speed_scale = scale

    def update(self, dt):
        step = SPEED * self.speed_scale * dt
        for a in AXES:
            d = self.t[a] - self.p[a]
            if d:
                self.p[a] = self.t[a] if abs(d) <= step else self.p[a] + (step if d > 0 else -step)
                self.joints[a].setJointPosition(self.p[a])
