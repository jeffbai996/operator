"""Safe public run diagnostics, including legacy stored errors."""
import re
import subprocess


def public_text(value):
    if not isinstance(value, str):
        return value
    if ('app-server' in value and ('Command ' in value or 'mcp_servers.' in value)):
        return 'The agent process did not close normally. Check the saved response before continuing.'
    # Drop the whole diagnostic containing a credential; regex replacement of
    # an individual value is unsafe with escaped/nested command-line quoting.
    if re.search(r'(?i)(OPERATOR_RUN_CREDENTIAL|authorization\s*[:=]|(?:api[_-]?key|access[_-]?token|password)\s*["\s]*[:=])', value):
        return 'Private diagnostic details were removed.'
    return value


def public_data(value):
    if isinstance(value, dict):
        return {k: public_data(v) for k, v in value.items()}
    if isinstance(value, list):
        return [public_data(v) for v in value]
    return public_text(value)


def run_error(exc):
    # Subprocess exception strings embed argv, including per-run credentials.
    if isinstance(exc, subprocess.TimeoutExpired):
        return 'The agent process timed out. Check the saved response before continuing.'
    if isinstance(exc, subprocess.SubprocessError):
        return 'The agent process failed. Check the saved response before continuing.'
    if isinstance(exc, OSError):
        return 'The agent process could not be started or contacted.'
    return public_text(str(exc))[:400] or 'The agent run failed.'


def quiet_closed_target_futures(loop, context):
    """Event-loop exception handler for loops that drive Playwright.

    Closing a page or connection rejects Playwright's in-flight protocol
    futures with TargetClosedError. When the awaiting call was already
    cancelled (an attach timeout, a teardown), nothing holds those futures and
    asyncio logs each one as an error, burying the real failure. Only that
    case is dropped; a Task we created still reports, as does anything else.
    """
    exc = context.get('exception')
    if (context.get('message', '').startswith('Future exception was never retrieved')
            and type(exc).__name__ == 'TargetClosedError'):
        return
    loop.default_exception_handler(context)
