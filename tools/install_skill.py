"""Install the optional Local Dream Desktop skill into Claude Code or Codex."""
import argparse
import os
from pathlib import Path
import re
import sys
import tempfile

MARKER = "<!-- Installed by Local Dream Desktop tools/install_skill.py -->"
ROOT = Path(__file__).resolve().parents[1]


def render_skill(root):
    """Resolve commands against this source tree or extracted portable package."""
    root = Path(root).resolve()
    executable = next((root / name for name in ("ldd.exe", "ldd") if (root / name).is_file()), None)
    if executable is not None:
        # Quoted executable paths require the call operator in PowerShell.
        command = ('& ' if os.name == "nt" else '') + '"{}" --cli'.format(executable.as_posix())
    elif (root / "sdgen.py").is_file():
        command = 'python "{}/sdgen.py"'.format(root.as_posix())
    else:
        raise ValueError("Run the installer from a complete source tree or extracted package")
    template = root / ".claude" / "skills" / "local-dream" / "SKILL.md"
    return (template.read_text(encoding="utf-8")
            .replace("{{LDPC_DIR}}", root.as_posix())
            .replace("{{LDPC_CLI}}", command).rstrip() + "\n\n" + MARKER + "\n")


def install(args):
    target = Path.home() / (".codex" if args.codex else ".claude") / "skills" / "local-dream" / "SKILL.md"
    # Never follow links when overwriting or removing an installed skill.
    if any(path.is_symlink() for path in (target, *target.parents)):
        raise ValueError("Refusing a symlinked installation target")
    existing = target.read_text(encoding="utf-8") if target.exists() else None
    # Accept this installer's previous product label when upgrading a managed copy.
    owned = existing is not None and any(re.fullmatch(
        r"<!-- Installed by Local Dream (?:PC|Desktop) tools/install_skill\.py -->", line
    ) for line in existing.splitlines())
    if args.uninstall:
        if existing is None:
            print("Local Dream Desktop skill is not installed; nothing to remove")
        elif not owned:
            raise ValueError("Refusing to uninstall a skill not installed by this tool")
        elif args.dry_run:
            print("Would remove {}".format(target))
        else:
            target.unlink()
            # Keep the directory and any user-created companion files.
            print("Removed {}".format(target))
        return
    content = render_skill(ROOT)
    if existing == content:
        print("Local Dream Desktop skill already up to date: {}".format(target))
        return
    if existing is not None and not owned and not args.force:
        raise ValueError("Target contains a different unmanaged skill; use --force to replace it")
    if args.dry_run:
        print("Would {} {} (source: {})".format("replace" if existing is not None else "install", target, ROOT))
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    print("Installed Local Dream Desktop skill: {}".format(target))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", action="store_true", help="Install to ~/.codex instead of ~/.claude")
    parser.add_argument("--force", action="store_true", help="Replace a different unmanaged skill")
    parser.add_argument("--dry-run", action="store_true", help="Describe changes without writing")
    parser.add_argument("--uninstall", action="store_true", help="Remove only a copy installed by this tool")
    args = parser.parse_args()
    try:
        install(args)
    except (OSError, UnicodeError, ValueError) as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
