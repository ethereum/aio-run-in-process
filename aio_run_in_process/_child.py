import argparse
import asyncio
import logging
import os
import signal
import sys
from typing import (
    Any,
    BinaryIO,
    Callable,
    Coroutine,
    Sequence,
    cast,
)

from ._utils import (
    RemoteException,
    pickle_value,
    receive_pickled_value,
)
from .state import (
    State,
)
from .typing import (
    TReturn,
)

logger = logging.getLogger("asyncio_run_in_process")


#
# CLI invocation for subprocesses
#
parser = argparse.ArgumentParser(description="asyncio-run-in-process")
parser.add_argument(
    "--parent-pid", type=int, required=True, help="The PID of the parent process"
)
parser.add_argument(
    "--fd-read",
    type=int,
    required=True,
    help=(
        "The file descriptor that the child process can use to read data that "
        "has been written by the parent process"
    ),
)
parser.add_argument(
    "--fd-write",
    type=int,
    required=True,
    help=(
        "The file descriptor that the child process can use for writing data "
        "meant to be read by the parent process"
    ),
)


def update_state(to_parent: BinaryIO, state: State) -> None:
    to_parent.write(state.value.to_bytes(1, 'big'))
    to_parent.flush()


def update_state_initialized(to_parent: BinaryIO) -> None:
    payload = State.INITIALIZED.value.to_bytes(1, 'big') + os.getpid().to_bytes(4, 'big')
    to_parent.write(payload)
    to_parent.flush()


def update_state_finished(to_parent: BinaryIO, finished_payload: bytes) -> None:
    payload = State.FINISHED.value.to_bytes(1, 'big') + finished_payload
    to_parent.write(payload)
    to_parent.flush()


async def _do_task_cleanup(*tasks: 'asyncio.Future[Any]') -> None:
    for task in tasks:
        if not task.done():
            task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass


SHUTDOWN_SIGNALS = {signal.SIGTERM}


async def _handle_SIGTERM(task: 'asyncio.Task[TReturn]', fut: 'asyncio.Future[int]') -> None:
    signum = await fut
    raise SystemExit(signum)


async def _handle_coro(coro: Coroutine[Any, Any, TReturn], got_SIGINT: asyncio.Event) -> TReturn:
    """
    Understanding this function requires some detailed knowledge of how
    coroutines work.

    The goal here is to run a coroutine function and wait for the result.
    However, if a SIGINT signal is received then we want to inject a
    `KeyboardInterrupt` exception into the running coroutine.

    Some additional nuance:

    - The `SIGINT` signal can happen multiple times and each time we want to
      throw a `KeyboardInterrupt` into the running coroutine which may choose to
      ignore the exception and continue executing.
    - When the `KeyboardInterrupt` hits the coroutine it can return a value
      which is sent using a `StopIteration` exception.  We treat this as the
      return value of the coroutine.
    """
    # The `coro` is first wrapped in `asyncio.shield` to protect us in the case
    # that a SIGINT happens.  In this case, the coro has a chance to return a
    # value during exception handling, in which case we are left with
    # `coro_task` which has not been awaited and thus will cause asyncio to
    # issue a warning.  However, since the coroutine has already exited, if we
    # await the `coro_task` then we will encounter a `RuntimeError`.  By
    # wrapping the coroutine in `asyncio.shield` we side-step this by
    # preventing the cancellation to actually penetrate the coroutine, allowing
    # us to await the `coro_task` without causing the `RuntimeError`.
    coro_task = asyncio.ensure_future(asyncio.shield(coro))
    while True:
        # Run the coroutine until it either returns, or a SIGINT is received.
        # This is done in a loop because the function *could* choose to ignore
        # the `KeyboardInterrupt` and continue processing, in which case we
        # reset the signal and resume waiting.
        done, pending = await asyncio.wait(
            (coro_task, got_SIGINT.wait()),
            return_when=asyncio.FIRST_COMPLETED,
        )

        if coro_task.done():
            await _do_task_cleanup(*pending)
            return await coro_task
        elif got_SIGINT.is_set():
            got_SIGINT.clear()

            # In the event that a SIGINT was recieve we need to inject a
            # KeyboardInterrupt exception into the running coroutine.
            try:
                coro.throw(KeyboardInterrupt)
            except StopIteration as err:

                # We need to clean up the actual coroutine which is a little
                # dirty.  It has been wrapped in an `asyncio.Task` so we get at
                # it via the returned `pending` tasks.
                await _do_task_cleanup(*pending)

                # StopIteration is how coroutines signal their return values.
                # If the exception was raised, we treat the argument as the
                # return value of the function.
                return cast(TReturn, err.value)
            except BaseException as err:
                raise
        else:
            raise Exception("Code path should not be reachable")


async def _do_async_fn(
    async_fn: Callable[..., Coroutine[Any, Any, TReturn]],
    args: Sequence[Any],
    to_parent: BinaryIO,
    loop: asyncio.AbstractEventLoop,
) -> TReturn:
    # state: STARTED
    update_state(to_parent, State.STARTED)

    # A Future that will be set if any of the SHUTDOWN_SIGNALS signals are
    # received causing _do_async_fn to raise a SystemExit
    system_exit_signum: 'asyncio.Future[int]' = asyncio.Future()

    # setup signal handlers.
    for signum in SHUTDOWN_SIGNALS:
        loop.add_signal_handler(
            signum.value,
            system_exit_signum.set_result,
            signum,
        )

    # state: EXECUTING
    update_state(to_parent, State.EXECUTING)

    # Install a signal handler to set an asyncio.Event upon receiving a SIGINT
    got_SIGINT = asyncio.Event()
    loop.add_signal_handler(
        signal.SIGINT,
        got_SIGINT.set,
    )

    # First we need to generate a coroutine.  We need this so we can throw
    # exceptions into the running coroutine to allow it to handle keyboard
    # interrupts.
    async_fn_coro: Coroutine[Any, Any, TReturn] = async_fn(*args)

    # The coroutine is then given to `_handle_coro` which waits for either the
    # coroutine to finish, returning the result, or for a SIGINT signal at
    # which point injects a `KeyboardInterrupt` into the running coroutine.
    async_fn_task: 'asyncio.Future[TReturn]' = asyncio.ensure_future(
        _handle_coro(async_fn_coro, got_SIGINT),
    )

    # Now we wait for either a result from the coroutine or a SIGTERM which
    # triggers immediate cancellation of the running coroutine.
    done, pending = await asyncio.wait(
        (async_fn_task, system_exit_signum),
        return_when=asyncio.FIRST_COMPLETED,
    )

    # We prioritize the `SystemExit` case.
    if system_exit_signum.done():
        await _do_task_cleanup(async_fn_task)
        raise SystemExit(system_exit_signum.result())
    elif async_fn_task.done():
        return async_fn_task.result()
    else:
        raise Exception("unreachable")


def _run_process(parent_pid: int, fd_read: int, fd_write: int) -> None:
    """
    Run the child process.

    This communicates the status of the child process back to the parent
    process over the given file descriptor, runs the coroutine, handles error
    cases, and transmits the result back to the parent process.
    """
    # state: INITIALIZING (default initial state)
    with os.fdopen(fd_write, "wb") as to_parent:
        # state: INITIALIZED
        update_state_initialized(to_parent)

        with os.fdopen(fd_read, "rb", closefd=True) as from_parent:
            # state: WAIT_EXEC_DATA
            update_state(to_parent, State.WAIT_EXEC_DATA)
            async_fn, args = receive_pickled_value(from_parent)

        # state: BOOTING
        update_state(to_parent, State.BOOTING)

        loop = asyncio.get_event_loop()

        try:
            try:
                result = loop.run_until_complete(
                    _do_async_fn(async_fn, args, to_parent, loop),
                )
            except BaseException:
                _, exc_value, exc_tb = sys.exc_info()
                # `mypy` thingks that `exc_value` and `exc_tb` are `Optional[..]` types
                remote_exc = RemoteException(exc_value, exc_tb)  # type: ignore
                finished_payload = pickle_value(remote_exc)
                raise
            finally:
                # state: STOPPING
                update_state(to_parent, State.STOPPING)
        except KeyboardInterrupt:
            code = 2
        except SystemExit as err:
            code = err.args[0]
        except BaseException:
            code = 1
        else:
            finished_payload = pickle_value(result)
            code = 0
        finally:
            # state: FINISHED
            update_state_finished(to_parent, finished_payload)
            sys.exit(code)


if __name__ == "__main__":
    args = parser.parse_args()
    _run_process(
        parent_pid=args.parent_pid, fd_read=args.fd_read, fd_write=args.fd_write
    )
