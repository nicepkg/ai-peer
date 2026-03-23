"""CLI dispatch — thin router to operation modules."""
from .helpers import get_or_create_identity
from .ops_room import cmd_daemon, cmd_room
from .ops_chat import cmd_chat
from .ops_ai import cmd_invite, cmd_quick, cmd_discuss
from .ops_peer import cmd_discover, cmd_list, cmd_register, cmd_history, cmd_export


def run_command(args):
    """Main dispatch."""
    if not args:
        raise ValueError(
            "Usage: peer <command> [args]\n\n"
            "Room & Chat:\n"
            "  room      create, join, list, delete rooms\n"
            "  chat      send/read messages, interactive mode\n"
            "  history   view conversation history\n"
            "  export    export room as Markdown or JSON\n\n"
            "AI:\n"
            "  invite    spawn an AI tool into a room\n"
            "  quick     one-command AI conversation (create+send+invite)\n"
            "  discuss   multi-round AI debate (auto-orchestrated)\n"
            "  discover  find installed AI CLI tools\n\n"
            "System:\n"
            "  daemon    start, stop, status\n"
            "  identity  show current identity\n"
            "  list      show all known peers\n"
            "  register  manually add a remote peer"
        )

    cmd, rest = args[0], args[1:]
    dispatch = {
        "daemon": cmd_daemon,
        "room": cmd_room,
        "chat": cmd_chat,
        "invite": cmd_invite,
        "quick": cmd_quick,
        "discuss": cmd_discuss,
        "discover": cmd_discover,
        "list": cmd_list,
        "register": cmd_register,
        "history": cmd_history,
        "export": cmd_export,
        "identity": lambda _: get_or_create_identity(),
    }

    handler = dispatch.get(cmd)
    if not handler:
        raise ValueError(f"Unknown command: {cmd}. Available: {', '.join(dispatch)}")
    return handler(rest)
