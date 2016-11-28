from . import world, util
import numpy as np
import itertools as it


def _is_collision(subject, others):
    '''Test for collisions.

    subject -- a (subclass of a) world.Body instance to test

    others -- a list of world.Body instances to test against

    returns -- `True` if the subject has collided with any object in the
    list 'others'.

    '''
    for other in others:
        if other is not subject:
            d_sq = ((other.position - subject.position) ** 2).sum()
            if d_sq < (subject.radius + other.radius) ** 2:
                return True
    return False


class _Destroy(Exception):
    '''Raised when an object should be destroyed.
    '''
    pass


def _move_body(subject, world_, control, dt):
    '''Compute the new body state of the subject.

    subject -- a world {Ship, Pellet, Planet}

    world_ -- the world to use for collisions, etc.

    control -- Control.STATE to use to update a Ship

    dt -- timestep
    '''

    # 1. Test for collisions - except planets, which cannot collide
    if (isinstance(subject, (world.Ship, world.Pellet)) and
            _is_collision(subject, world_.objects.values())):
        raise _Destroy()

    # 2. Compute the new position & velocity
    accel = util.Vector.zero()

    if control is not None:
        forward = subject.max_thrust * np.clip(control['thrust'], 0, 1)
        accel += forward * util.direction(subject.orientation)

    # (massless objects experience no gravity)
    if subject.mass != 0:
        for other in world_.objects.values():
            if other is not subject:
                relative = other.position - subject.position
                accel += (
                    world_.space.gravity * other.mass / (relative ** 2).sum()
                ) * relative

    new_velocity = subject.velocity + dt * accel

    new_position = (subject.position +
                    (dt / 2) * subject.velocity +
                    (dt / 2) * new_velocity)

    # Ships should wrap around the world
    if isinstance(subject, world.Ship):
        new_position = new_position % world_.space.dimensions

    # Pellets are auto-destroyed when out-of-bounds (for efficiency)
    if isinstance(subject, world.Pellet) and \
       np.any(new_position < util.Vector.zero()) and \
       np.any(world_.space.dimensions <= new_position):
        raise _Destroy

    # 3. Compute the new orientation
    if control is not None:
        rotate = subject.max_rotate * np.clip(control['rotate'], -1, 1)
        new_orientation = (subject.orientation + dt * rotate) % (2 * np.pi)
    else:
        new_orientation = subject.orientation

    return dict(position=util.Vector.to_log(new_position),
                velocity=util.Vector.to_log(new_velocity),
                orientation=new_orientation)


def _update_weapon(weapon, control_fire, dt):
    '''Compute the update from a weapon - update temperature & reload,
    and return whether the weapon is actually able to fire.

    weapon -- a world.Weapon object to be updated

    control_fire -- True if the controller requests the weapon to firs

    dt -- timestep

    returns (weapon_state, fired)
        weapon_state -- the new state of the weapon
        fired -- true if the weapon was fired
    '''
    reload = max(0, weapon.reload - dt)
    # Calculate the decay ratio needed to get the time spent above
    # max_temperature to == weapon.temperature_decay
    mr = weapon.max_temperature / (weapon.max_temperature + 1)
    decay_ratio = mr ** (dt / weapon.temperature_decay)
    temperature = decay_ratio * weapon.temperature

    fired = (control_fire and
             reload == 0 and
             temperature < weapon.max_temperature)
    if fired:
        reload = weapon.max_reload
        temperature += 1

    return dict(reload=reload, temperature=temperature), fired


def _fire_pellet(ship, body_state):
    '''Compute the 'create' event for a single pellet fired
    from a ship.

    ship -- the world.Ship that is firing

    body_state -- the ship's next body state (N.B. we use this to
    avoid "shooting ourselves" errors)

    returns -- a Pellet.CREATE event
    '''
    direction = util.direction(body_state['orientation'])
    # Use a small "fudge ratio" to spawn the pellet further away from the ship
    # (so we don't shoot ourselves).
    position = (util.Vector.create(body_state['position']) +
                1.01 * ship.radius * direction)
    velocity = (util.Vector.create(body_state['velocity']) +
                ship.weapon.speed * direction)
    return dict(
        body=dict(
            mass=0.0,
            radius=0.0,
            state=dict(
                position=util.Vector.to_log(position),
                velocity=util.Vector.to_log(velocity),
                orientation=body_state['orientation'],
            )),
        time_to_live=ship.weapon.time_to_live)


class Simulator:
    '''A simulator computes a single step, based on a world
    (which should be updated externally).
    '''
    def __init__(self, world, step_duration, object_id_gen):
        self._world = world
        self._step_duration = step_duration
        self._object_id_gen = object_id_gen

    def _update_object(self, id, control):
        obj = self._world.objects[id]
        try:
            state = dict(body=_move_body(obj,
                                         world_=self._world,
                                         control=control,
                                         dt=self._step_duration))

            if isinstance(obj, world.Ship):
                state['controller'] = control

                # TODO: change weapon state to have a 'firing' flag
                # - simplifies this code (& makes sense)
                state['weapon'], fired = _update_weapon(obj.weapon,
                                                        control['fire'],
                                                        dt=self._step_duration)
                if fired:
                    yield dict(id=next(self._object_id_gen),
                               data=_fire_pellet(obj, state['body']))

            elif isinstance(obj, world.Pellet):
                state['time_to_live'] = obj.time_to_live - self._step_duration
                if state['time_to_live'] <= 0:
                    raise _Destroy()

            elif isinstance(obj, world.Planet):
                pass  # nothing else to update

            else:
                raise ValueError('Unknown object type %s' % type(obj))

            yield dict(id=id, data=state)

        except _Destroy:
            yield dict(id=id, data=dict())

    def __call__(self, controller_states):
        '''Return a list of events corresponding a single step of the simulation.
        '''
        return [event
                for id in self._world.objects
                for event in self._update_object(
                        id, controller_states.get(id))]


class Controllers:
    DEFAULT_STATE = dict(
        fire=False,
        rotate=0.0,
        thrust=0.0,
    )

    def __init__(self, world_, ids_bots):
        self._world = world_
        self._ids_bots = ids_bots
        self.control = {id: Controllers.DEFAULT_STATE
                        for id, bot in ids_bots}

    def __call__(self, step):
        for id, bot in self._ids_bots:
            ship = self._world.objects.get(id)
            if ship is None:
                bot(dict(step=step, ship_id=None))
            else:
                self.control[id] = bot(dict(step=step, ship_id=id))


def run_game(map_spec, controller_bots, step_duration):
    '''Create an iterable of game updates.

    map_spec -- should have properties (space, planets, ship)

    controller_bots -- a list of pairs (.schema.Controller.CREATE, .bot.Bot),

    step_duration -- period of time per step

    returns -- a sequence of log events (according to .schema.STEP)
    by running the game.

    '''
    # Objects needed for running the game
    object_id_gen = it.count()
    world_ = world.World()
    world_.clock = -1  # Advances to zero on first step
    simulator = Simulator(world_, step_duration, object_id_gen)

    # The initial state
    planets = [dict(id=next(object_id_gen), data=planet)
               for planet in map_spec.planets]

    ships = [dict(id=next(object_id_gen),
                  data=map_spec.ship(dict(state=Controllers.DEFAULT_STATE,
                                          **controller)))
             for controller, _ in controller_bots]

    controllers = Controllers(world_, [
        (ship['id'], bot)
        for ship, (_, bot) in zip(ships, controller_bots)])

    # Helper function - create a 'step' & update the simulation state
    def step(data):
        step_ = dict(clock=world_.clock + 1,
                     duration=step_duration,
                     data=data)
        world_(step_)
        controllers(step_)
        return step_

    # Run the game
    yield step(map_spec.space)
    yield step(planets + ships)
    while world_.time < world_.space.lifetime:
        yield step(simulator(controllers.control))
