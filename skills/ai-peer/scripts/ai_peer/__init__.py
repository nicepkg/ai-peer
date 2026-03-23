"""AI Peer — decentralized AI-to-AI and human-AI communication."""
import json
import sys


def output(ok, data=None, error=None):
    """Standard output envelope."""
    result = {"ok": ok, "schema_version": "1"}
    if ok:
        result["data"] = data
    else:
        result["error"] = str(error)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if ok else 1)


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("Usage: peer <command> [args]\n")
        print("Room & Chat:")
        print("  room      create, join, list, delete rooms")
        print("  chat      send/read messages, interactive mode (-i)")
        print("  history   view conversation history")
        print("  export    export room as Markdown or JSON\n")
        print("AI:")
        print("  invite    spawn an AI tool into a room")
        print("  quick     one-command AI conversation (create+send+invite)")
        print("  discuss   multi-round AI debate (auto-orchestrated)")
        print("  discover  find installed AI CLI tools\n")
        print("System:")
        print("  daemon    start, stop, status")
        print("  identity  show current identity")
        print("  list      show all known peers")
        print("  register  manually add a remote peer")
        sys.exit(0)

    try:
        from .ops import run_command
        result = run_command(args)
        output(True, result)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        output(False, error=e)


if __name__ == "__main__":
    main()
