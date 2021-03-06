'''Define bot (AI) control interface to play the game.
'''

import fastavro
import sys
import subprocess
import random
import concurrent.futures
from . import schema, world


def _safe_flush(writer):
    writer.flush()
    writer.block_count = 0  # due to a bug in fastavro (PR #64)


class Bot:
    '''A player-defined bot for controlling a ship in the game.
    '''
    REQUEST = dict(
        type='record',
        name='Request',
        namespace='photonai.bot',
        fields=[
            dict(name='step', type=schema.STEP,
                 doc='a game engine step - may not be "complete",'
                 ' e.g. if an enemy ship is obscured'),
            dict(name='ship_id', type=['int', 'null']),
        ])

    RESPONSE = [schema.Controller.STATE, 'null']

    def __call__(self, request):
        '''Run the bot for a single game step, and get the control output
        to control the Ship object ship_id.

        request -- schema.REQUEST -- the request for the controller - a
        step from the game engine, and the ship ID for which control is
        requested. Or `None`, in which case the bot is finished & should
        be destroyed

        returns -- Bot.RESPONSE -- the control to apply for the ship
        '''
        raise NotImplementedError

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        '''Dispose any resources associated with this bot.
        '''
        pass

    def run_loop(self):
        '''Run a loop, listening for requests on STDIN, and writing control
        responses to STDOUT.
        '''
        writer = fastavro._writer.Writer(sys.stdout.buffer, Bot.RESPONSE)
        # Make sure no-one else writes to stdout - that would be bad!
        sys.stdout = sys.stderr
        _safe_flush(writer)
        for request in fastavro.reader(sys.stdin.buffer):
            writer.write(self(request))
            _safe_flush(writer)


class SimpleBot(Bot):
    '''A bot that presents an useful interface to subclasses.

    Accumulates game state updates into a `photonai.world.World`, and
    provides a simple way to respond with control signals `SimpleBot.Control`.

    Subclasses should implement `control = self.get_control(world, ship)`,
    which is called at each timestep to control the ship.
    '''

    class Control:
        '''The control signal returned from a `SimpleBot`.
        '''
        __slots__ = ('fire', 'rotate', 'thrust')

        def __init__(self, fire=False, rotate=0.0, thrust=0.0):
            self.fire = fire
            self.rotate = rotate
            self.thrust = thrust

        def to_log(self):
            return dict(fire=bool(self.fire),
                        rotate=float(self.rotate),
                        thrust=float(self.thrust))

    def __init__(self):
        self._world = world.World()

    def __call__(self, request):
        self._world(request['step'])
        if request['ship_id'] is not None:
            ship = self._world.objects[request['ship_id']]
            return self.get_control(self._world, ship).to_log()

    def get_control(self, world, ship):
        '''Get the control signal for 'ship' in the current state of 'world'.
        This is called for each game tick (as per `config["step_duration"]`).

        If this method throws an error, or takes too long to return (longer
        than `config["timeout"]`), the ship will "hold" the last control signal
        returned until the end of the game (the controller will not be given a
        chance to recover).

        `world` -- a `photonai.world.World` object containing all known state.
        Note that the state of some ships may be out-of-date if they
        are currently obscured by planets - this can be determined by testing
        if the `obj.update_clock` of an object in the world lags the
        `world.clock`.

        `ship` -- a `photonai.world.Ship` object for the ship being controlled.

        `returns` -- a `SimpleBot.Control` object for the ship being controlled
        e.g. `SimpleBot.Control(fire=True, rotate=-0.2, thrust=0.6)`.

        '''
        raise NotImplementedError


class SubprocessBot(Bot):
    '''A bot that forwards to an Avro stdin/stdout streaming subprocess.
    '''
    def __init__(self, command, stderr, timeout):
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr)
        self._request = fastavro._writer.Writer(
            self._process.stdin, Bot.REQUEST)
        _safe_flush(self._request)
        self._response = fastavro.reader(self._process.stdout)
        self._pool = concurrent.futures.ThreadPoolExecutor(1)
        self._timeout = timeout

    def close(self):
        self._pool.shutdown()
        try:
            self._process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            self._process.kill()

    def execute(self, request):
        self._request.write(request)
        _safe_flush(self._request)
        return next(self._response)

    def __call__(self, request):
        return self._pool.submit(self.execute, request).result(self._timeout)


class DockerPythonBot(SubprocessBot):
    '''A bot that forwards to an Avro stdin/stdout streaming subprocess
    in Docker.
    '''
    def __init__(self, path, container, **args):
        self._container_name = 'bot-%x' % random.randint(0, 1 << 32)
        super().__init__(
            command=['docker', 'run', '--rm', '-i',
                     '-v', '%s:/bot.py' % path,
                     '--name', self._container_name,
                     container, 'python3', '/bot.py'],
            **args)

    def close(self):
        try:
            subprocess.check_call(['docker', 'kill', self._container_name])
        except subprocess.CalledProcessError:
            pass  # ignore closing errors - best effort
        super().close()
