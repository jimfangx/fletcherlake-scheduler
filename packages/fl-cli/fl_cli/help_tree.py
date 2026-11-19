"""Show nested CLI commands and their required inputs without invoking callbacks."""

from typing import Any

from typer.core import TyperGroup


class CommandTreeGroup(TyperGroup):
    def format_help(self, ctx: Any, formatter: Any) -> None:
        # Use the standard help formatter so the command tree replaces the flat list.
        self.format_usage(ctx, formatter)
        self.format_help_text(ctx, formatter)
        self.format_options(ctx, formatter)
        self.format_epilog(ctx, formatter)

    def format_commands(self, ctx: Any, formatter: Any) -> None:
        lines: list[str] = []

        def visit(group: Any, prefix: str = "") -> None:
            commands = [(name, cmd) for name, cmd in group.commands.items() if not cmd.hidden]
            for index, (name, command) in enumerate(commands):
                last = index == len(commands) - 1
                label = name
                for parameter in command.get_params(ctx):
                    if getattr(parameter, "hidden", False):
                        continue
                    if parameter.param_type_name == "argument":
                        argument = parameter.human_readable_name.upper()
                        if parameter.nargs != 1:
                            argument += "..."
                        label += " " + (argument if parameter.required else f"[{argument}]")
                    elif parameter.required:
                        label += " " + parameter.opts[0]
                        if not parameter.is_flag:
                            label += " " + parameter.make_metavar(ctx)
                description = command.get_short_help_str()
                if description:
                    label += "  " + description
                lines.append(prefix + ("└── " if last else "├── ") + label)
                if isinstance(command, TyperGroup):
                    visit(command, prefix + ("    " if last else "│   "))

        visit(self)
        if lines:
            with formatter.section("Commands"):
                formatter.write_text("\b\n" + "\n".join(lines))
