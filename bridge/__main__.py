"""Run `python -m bridge` for HTTP, optional HTTPS and coordinated shutdown.
`python -m bridge announce` advertises Bonjour only (bridge/announce.py)."""
import asyncio
import logging
import os
import signal
import sys

from aiohttp import web

from .i18n import m
from .auth import AuthError
from .config import Config, ConfigError
from .identity import IdentityError, load_identity
from .server import create_app
from .tls import TLSError, prepare_tls


async def serve(config: Config) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    identity = load_identity(config.auth_dir / "bridge.json", config.bridge_name)
    tls_state = prepare_tls(config, identity)
    runner = web.AppRunner(create_app(config, identity=identity, tls_state=tls_state), access_log=None)
    await runner.setup()
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    for caught in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(caught, stopped.set)
            installed.append(caught)
        except NotImplementedError:
            pass
    try:
        http = web.TCPSite(runner, "0.0.0.0", config.port)
        await http.start()
        logging.getLogger(__name__).info(m('HTTP listening on port %d'), config.port)
        if tls_state.context:
            https = web.TCPSite(runner, "0.0.0.0", config.https_port, ssl_context=tls_state.context)
            await https.start()
            logging.getLogger(__name__).info(m('HTTPS listening on port %d'), config.https_port)
        await stopped.wait()
    finally:
        for caught in installed:
            loop.remove_signal_handler(caught)
        await runner.cleanup()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if sys.argv[1:2] == ["announce"]:
        # Bonjour announcement only, in a second host-networked container (bridge/announce.py).
        from .announce import run
        try:
            asyncio.run(run(dict(os.environ)))
        except ConfigError as exc:
            print(m('Configuration: {p0}', p0=exc), file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            pass
        return 0
    try:
        config = Config.from_env()
        asyncio.run(serve(config))
    except (ConfigError, IdentityError, AuthError, TLSError, OSError) as exc:
        print(m('Configuration: {p0}', p0=exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
