import argparse
import asyncio
import base64
import json
import shlex
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]
MAX_RESPONSE_BYTES = 1_048_576


class ServerRequestError(Exception):
    pass


class ConsoleProtocolClient:
    def __init__(self, host: str, port: int, *, event_handler: EventHandler | None = None) -> None:
        self.host = host
        self.port = port
        self.event_handler = event_handler
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_request_id = 1
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        self.reader, self.writer = await asyncio.open_connection(
            self.host, self.port, limit=MAX_RESPONSE_BYTES
        )
        self._reader_task = asyncio.create_task(self._read_loop(), name="console-client-reader")

    async def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
            await self.writer.wait_closed()
            self.writer = None
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None

    async def request(self, action: str, **params: Any) -> Any:
        if self.writer is None:
            raise ConnectionError("Not connected to the server")
        request_id = self._next_request_id
        self._next_request_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message = json.dumps(
            {"id": request_id, "action": action, "params": params},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            async with self._write_lock:
                self.writer.write(message.encode("utf-8") + b"\n")
                await self.writer.drain()
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def _read_loop(self) -> None:
        assert self.reader is not None
        try:
            while not self.reader.at_eof():
                raw = await self.reader.readline()
                if not raw:
                    break
                message = json.loads(raw)
                if "event" in message:
                    if self.event_handler is not None:
                        await self.event_handler(message)
                    continue
                request_id = message.get("id")
                future = self._pending.get(request_id)
                if future is None or future.done():
                    continue
                if message.get("ok"):
                    future.set_result(message.get("result"))
                else:
                    error = message.get("error", {})
                    future.set_exception(
                        ServerRequestError(error.get("message", "Server rejected the request"))
                    )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fail_pending(error)
        finally:
            self._fail_pending(ConnectionError("Server connection closed"))

    def _fail_pending(self, error: BaseException) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)


class InteractiveConsole:
    def __init__(self, client: ConsoleProtocolClient) -> None:
        self.client = client
        self.current_tournament_id: str | None = None
        self.current_lobby_id: str | None = None
        self.current_game_id: str | None = None
        self.current_appeal_id: str | None = None
        self.logged_in = False

    async def event(self, message: dict[str, Any]) -> None:
        event = message["event"]
        if event == "lobby_readiness_changed":
            state = "ready" if message["ready"] else "not ready"
            print(
                f"\n{message['player_name']} is {state}. "
                f"{message['ready_count']}/{message['player_count']} players are ready."
            )
        elif event == "lobby_changed":
            self.current_lobby_id = message["lobby_id"]
            self.current_tournament_id = message["snapshot"]["tournament_id"]
            print("\n[Lobby changed]")
            self._show_lobby(message["snapshot"])
        elif event == "game_changed":
            self.current_game_id = message["game_id"]
            self._update_game_context(message["snapshot"])
            for item in message.get("events", []):
                self._show_game_event(item)
            self._show_game(message["snapshot"])
        elif event == "player_buzzed":
            player = message["player"]
            print(f"\n[Buzz] {player['display_name']} buzzed")
        elif event == "player_answered":
            player = message["player"]
            print(f"\n[Answer] {player['display_name']}: {message['answer']}")
        print("> ", end="", flush=True)

    async def run(self) -> None:
        self._help()
        while True:
            try:
                raw = await asyncio.to_thread(input, "> ")
            except EOFError:
                return
            try:
                if not await self.execute(raw):
                    return
            except (ConnectionError, ServerRequestError, ValueError) as error:
                print(f"Rejected: {error}")
            except OSError as error:
                print(f"I/O error: {error}")

    async def execute(self, raw: str) -> bool:
        arguments = shlex.split(raw)
        if not arguments:
            return True
        command = arguments.pop(0).casefold()
        if command == "quit":
            return False
        if command == "help":
            self._help()
            return True
        if command == "login":
            if len(arguments) < 2:
                raise ValueError('Usage: login <player-id> "<display-name>" [--admin [token]]')
            player_id = int(arguments.pop(0))
            admin = "--admin" in arguments
            admin_token = None
            if admin:
                admin_index = arguments.index("--admin")
                trailing = arguments[admin_index + 1 :]
                if len(trailing) > 1:
                    raise ValueError("Only one administrator token may be supplied")
                admin_token = trailing[0] if trailing else None
                arguments = arguments[:admin_index]
            display_name = " ".join(arguments)
            result = await self.client.request(
                "login",
                telegram_user_id=player_id,
                display_name=display_name,
                admin=admin,
                admin_token=admin_token,
            )
            self.logged_in = True
            self._update_context(result)
            self._show_json(result)
            return True
        self._require_login()
        if command == "me":
            result = await self.client.request("me")
            self._update_context(result)
            self._show_json(result)
        elif command == "tournament":
            await self._tournament(arguments)
        elif command == "packets":
            if arguments:
                raise ValueError("Usage: packets")
            tournament_id = self._tournament_id()
            self._show_packets(await self.client.request("packets", tournament_id=tournament_id))
        elif command == "lobby":
            await self._lobby(arguments)
        elif command == "blacklist":
            await self._blacklist(arguments)
        elif command == "game":
            await self._game(arguments)
        elif command == "appeals":
            await self._appeals(arguments)
        elif command == "packet":
            await self._packet_admin(arguments)
        elif command == "admin":
            await self._admin(arguments)
        else:
            raise ValueError("Unknown command; use 'help'")
        return True

    async def _tournament(self, arguments: list[str]) -> None:
        if not arguments:
            raise ValueError(
                "Usage: tournament "
                "<list|create|use|info|member|manager|setting|mutable|policy|requirement|packet>"
            )
        operation = arguments.pop(0).casefold()
        if operation == "list":
            if len(arguments) != 1 or arguments[0].casefold() not in {"mine", "public"}:
                raise ValueError("Usage: tournament list <mine|public>")
            scope = arguments[0].casefold()
            action = "tournaments_mine" if scope == "mine" else "tournaments_public"
            empty_message = (
                "No tournament memberships" if scope == "mine" else "No public tournaments"
            )
            listing = await self.client.request(action)
            self._show_tournament_listing(listing, empty_message=empty_message)
            return
        if operation == "create":
            if len(arguments) < 3:
                raise ValueError(
                    'Usage: tournament create <token> <slug> "<name>" '
                    "[--type <type>] [--ruleset <ruleset>] [--visibility <public|private>] "
                    "[--language <tag>] [--payment-type <type>] "
                    "[--pricing-plans <json>] [--registration-open <on|off>] "
                    "[--registration-starts-at <timestamp>] "
                    "[--registration-ends-at <timestamp>] [--starts-at <timestamp>] "
                    "[--planned-ends-at <timestamp>]"
                )
            token, slug, name = arguments[:3]
            options = arguments[3:]
            type_key = "ladder"
            ruleset = "si"
            visibility = "private"
            starts_at = None
            planned_ends_at = None
            option_values = {
                "--type": "type_key",
                "--ruleset": "ruleset",
                "--visibility": "visibility",
                "--starts-at": "starts_at",
                "--planned-ends-at": "planned_ends_at",
                "--language": "language",
                "--payment-type": "payment_type",
                "--pricing-plans": "pricing_plans",
                "--registration-open": "registration_open",
                "--registration-starts-at": "registration_starts_at",
                "--registration-ends-at": "registration_ends_at",
            }
            parsed: dict[str, str] = {}
            while options:
                flag = options.pop(0)
                destination = option_values.get(flag)
                if destination is None or not options:
                    raise ValueError(f"Unknown or incomplete tournament option: {flag}")
                parsed[destination] = options.pop(0)
            type_key = parsed.get("type_key", type_key)
            ruleset = parsed.get("ruleset", ruleset)
            visibility = parsed.get("visibility", visibility)
            starts_at = parsed.get("starts_at", starts_at)
            planned_ends_at = parsed.get("planned_ends_at", planned_ends_at)
            request = {
                "token": token,
                "slug": slug,
                "name": name,
                "type": type_key,
                "game_ruleset": ruleset,
                "visibility": visibility,
                "starts_at": starts_at,
                "planned_ends_at": planned_ends_at,
            }
            for name in (
                "language",
                "payment_type",
                "registration_starts_at",
                "registration_ends_at",
            ):
                if name in parsed:
                    request[name] = parsed[name]
            if "pricing_plans" in parsed:
                try:
                    request["pricing_plans"] = json.loads(parsed["pricing_plans"])
                except json.JSONDecodeError as error:
                    raise ValueError("--pricing-plans must contain valid JSON") from error
            if "registration_open" in parsed:
                request["registration_open"] = self._boolean_value(parsed["registration_open"])
            result = await self.client.request("tournament_create", **request)
            self.current_tournament_id = result["id"]
            self._show_json(result)
            return
        if operation in {"use", "manage"}:
            if len(arguments) != 1:
                raise ValueError(f"Usage: tournament {operation} <tournament-id>")
            result = await self.client.request(
                "tournament_manage" if operation == "manage" else "tournament_info",
                tournament_id=arguments[0],
            )
            self.current_tournament_id = result["id"]
            self._show_tournament(result)
            return
        if operation == "info":
            if arguments:
                raise ValueError("Usage: tournament info")
            result = await self._tournament_info()
            self._show_tournament(result)
            return
        if operation == "register":
            if len(arguments) > 1:
                raise ValueError("Usage: tournament register [tournament-id]")
            tournament_id = arguments[0] if arguments else self._tournament_id()
            result = await self.client.request("tournament_register", tournament_id=tournament_id)
            self.current_tournament_id = tournament_id
            self._show_json(result)
            return

        tournament_id = self._tournament_id()
        if operation == "member":
            if len(arguments) != 2 or arguments[0].casefold() != "add":
                raise ValueError("Usage: tournament member add <player-uuid>")
            result = await self.client.request(
                "tournament_member_add",
                tournament_id=tournament_id,
                player_id=arguments[1],
            )
        elif operation == "invite":
            if len(arguments) != 1:
                raise ValueError("Usage: tournament invite <player-uuid>")
            result = await self.client.request(
                "tournament_invite", tournament_id=tournament_id, player_id=arguments[0]
            )
        elif operation == "registrations":
            if arguments:
                raise ValueError("Usage: tournament registrations")
            result = await self.client.request(
                "tournament_registrations", tournament_id=tournament_id
            )
        elif operation == "approve":
            if len(arguments) != 1:
                raise ValueError("Usage: tournament approve <player-uuid|all>")
            if arguments[0].casefold() == "all":
                result = await self.client.request(
                    "tournament_registrations_approve_all", tournament_id=tournament_id
                )
            else:
                result = await self.client.request(
                    "tournament_registration_approve",
                    tournament_id=tournament_id,
                    player_id=arguments[0],
                )
        elif operation == "finalize":
            if arguments:
                raise ValueError("Usage: tournament finalize")
            info = await self._tournament_info()
            result = await self.client.request(
                "tournament_setup_finalize",
                tournament_id=tournament_id,
                expected_version=info["settings_version"],
            )
        elif operation == "start":
            if arguments:
                raise ValueError("Usage: tournament start")
            info = await self._tournament_info()
            result = await self.client.request(
                "tournament_start",
                tournament_id=tournament_id,
                expected_version=info["settings_version"],
            )
        elif operation == "stage":
            if (
                len(arguments) != 2
                or arguments[0].casefold() != "start"
                or arguments[1].casefold() not in {"first", "playoff"}
            ):
                raise ValueError("Usage: tournament stage start <first|playoff>")
            info = await self._tournament_info()
            result = await self.client.request(
                "tournament_stage_start",
                tournament_id=tournament_id,
                expected_version=info["settings_version"],
                kind=arguments[1].casefold(),
            )
        elif operation == "participants":
            if not arguments or arguments[0].casefold() != "finalize":
                raise ValueError(
                    "Usage: tournament participants finalize [approved-player-uuid ...]"
                )
            result = await self.client.request(
                "tournament_participants_finalize",
                tournament_id=tournament_id,
                player_ids=arguments[1:],
            )
        elif operation == "complete":
            if len(arguments) > 1:
                raise ValueError("Usage: tournament complete [timestamp]")
            result = await self.client.request(
                "tournament_complete",
                tournament_id=tournament_id,
                actual_ends_at=arguments[0] if arguments else None,
            )
        elif operation == "metadata":
            if len(arguments) != 2:
                raise ValueError("Usage: tournament metadata <field> <json-value>")
            field = arguments[0].replace("-", "_")
            try:
                value = json.loads(arguments[1])
            except json.JSONDecodeError:
                value = arguments[1]
            result = await self.client.request(
                "tournament_metadata_update", tournament_id=tournament_id, **{field: value}
            )
        elif operation == "pricing":
            if len(arguments) != 3 or arguments[0].casefold() != "set":
                raise ValueError(
                    "Usage: tournament pricing set <free|one-time|per-stage> <plans-json>"
                )
            try:
                pricing_plans = json.loads(arguments[2])
            except json.JSONDecodeError as error:
                raise ValueError("Pricing plans must contain valid JSON") from error
            result = await self.client.request(
                "tournament_metadata_update",
                tournament_id=tournament_id,
                payment_type=arguments[1],
                pricing_plans=pricing_plans,
            )
        elif operation == "requirement":
            if not arguments:
                raise ValueError("Usage: tournament requirement <list|add|remove> ...")
            requirement_operation = arguments.pop(0).casefold()
            if requirement_operation == "list" and not arguments:
                result = await self.client.request(
                    "tournament_registration_requirements",
                    tournament_id=tournament_id,
                )
            elif requirement_operation == "add" and len(arguments) >= 2:
                result = await self.client.request(
                    "tournament_registration_requirement_add",
                    tournament_id=tournament_id,
                    kind=arguments[0],
                    target_id=arguments[1],
                    failure_message=" ".join(arguments[2:]) or None,
                )
            elif requirement_operation == "remove" and len(arguments) == 1:
                result = await self.client.request(
                    "tournament_registration_requirement_remove",
                    tournament_id=tournament_id,
                    requirement_id=arguments[0],
                )
            else:
                raise ValueError(
                    "Usage: tournament requirement list | "
                    "tournament requirement add <kind> <target-id> [failure reason] | "
                    "tournament requirement remove <requirement-id>"
                )
        elif operation == "manager":
            if len(arguments) != 2 or arguments[0].casefold() not in {"add", "remove"}:
                raise ValueError("Usage: tournament manager <add|remove> <player-uuid>")
            result = await self.client.request(
                f"tournament_manager_{arguments[0].casefold()}",
                tournament_id=tournament_id,
                player_id=arguments[1],
            )
        elif operation == "setting":
            if len(arguments) != 2:
                raise ValueError("Usage: tournament setting <name> <value>")
            info = await self._tournament_info()
            defaults = dict(info["default_parameters"])
            defaults[arguments[0]] = self._setting_value(arguments[0], arguments[1])
            result = await self._update_tournament_policy(info, defaults=defaults)
        elif operation == "mutable":
            if len(arguments) != 2:
                raise ValueError("Usage: tournament mutable <setting> <on|off>")
            info = await self._tournament_info()
            mutable = set(info["player_mutable_parameters"])
            if self._boolean_value(arguments[1]):
                mutable.add(arguments[0])
            else:
                mutable.discard(arguments[0])
            result = await self._update_tournament_policy(info, mutable=sorted(mutable))
        elif operation == "policy":
            if len(arguments) != 2:
                raise ValueError("Usage: tournament policy <name> <json-value>")
            info = await self._tournament_info()
            policies = dict(info["policies"])
            try:
                policies[arguments[0]] = json.loads(arguments[1])
            except json.JSONDecodeError as error:
                raise ValueError("Tournament policy values must be valid JSON") from error
            result = await self._update_tournament_policy(info, policies=policies)
        elif operation == "packet":
            result = await self._tournament_packet(tournament_id, arguments)
        else:
            raise ValueError(f"Unknown tournament operation: {operation}")
        self._show_json(result)

    async def _tournament_packet(self, tournament_id: str, arguments: list[str]) -> dict[str, Any]:
        if not arguments:
            raise ValueError("Usage: tournament packet <assign|entitlement> ...")
        operation = arguments.pop(0).casefold()
        if operation == "assign":
            if not arguments:
                raise ValueError(
                    "Usage: tournament packet assign <packet-id> "
                    "[--version <version-id>] [--access-level <level>] "
                    "[--discoverable] [--playable] "
                    "[--content-visible] [--editable]"
                )
            packet_id = arguments.pop(0)
            version_id = None
            access_level = None
            rights = {
                "discoverable": False,
                "playable": False,
                "content_visible": False,
                "editable": False,
            }
            while arguments:
                flag = arguments.pop(0)
                if flag == "--version":
                    if not arguments:
                        raise ValueError("--version requires a packet version ID")
                    version_id = arguments.pop(0)
                    continue
                if flag == "--access-level":
                    if not arguments:
                        raise ValueError("--access-level requires a value")
                    access_level = arguments.pop(0)
                    continue
                right = flag.removeprefix("--").replace("-", "_")
                if not flag.startswith("--") or right not in rights:
                    raise ValueError(f"Unknown packet-assignment option: {flag}")
                rights[right] = True
            return await self.client.request(
                "tournament_packet_assign",
                tournament_id=tournament_id,
                packet_id=packet_id,
                packet_version_id=version_id,
                access_level=access_level,
                **rights,
            )
        if operation == "entitlement":
            if len(arguments) == 4 and arguments[2].replace("_", "-") == "access-level":
                assignment_id, player_id, _, access_level = arguments
                return await self.client.request(
                    "tournament_packet_entitlement_set",
                    assignment_id=assignment_id,
                    player_id=player_id,
                    rights={},
                    access_level=access_level,
                )
            if len(arguments) != 4:
                raise ValueError(
                    "Usage: tournament packet entitlement <assignment-id> <player-uuid> "
                    "<right> <on|off>"
                )
            assignment_id, player_id, right, raw_enabled = arguments
            normalized_right = right.replace("-", "_")
            if normalized_right not in {
                "discoverable",
                "playable",
                "content_visible",
                "editable",
            }:
                raise ValueError(f"Unknown packet entitlement: {right}")
            return await self.client.request(
                "tournament_packet_entitlement_set",
                assignment_id=assignment_id,
                player_id=player_id,
                rights={normalized_right: self._boolean_value(raw_enabled)},
            )
        raise ValueError(f"Unknown tournament packet operation: {operation}")

    async def _tournament_info(self) -> dict[str, Any]:
        tournament_id = self._tournament_id()
        result = await self.client.request("tournament_info", tournament_id=tournament_id)
        self.current_tournament_id = result["id"]
        return result

    async def _update_tournament_policy(
        self,
        info: dict[str, Any],
        *,
        defaults: dict[str, Any] | None = None,
        mutable: list[str] | None = None,
        policies: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self.client.request(
            "tournament_policy_update",
            tournament_id=info["id"],
            default_parameters=defaults or info["default_parameters"],
            player_mutable_parameters=(
                mutable if mutable is not None else info["player_mutable_parameters"]
            ),
            policies=policies if policies is not None else info["policies"],
        )
        return await self._tournament_info()

    async def _lobby(self, arguments: list[str]) -> None:
        if not arguments:
            raise ValueError("Usage: lobby <create|join|observe|role|info|packet|ready|start|...>")
        operation = arguments.pop(0).casefold()
        if operation == "create":
            maximum = int(arguments.pop(0)) if arguments else 4
            if arguments:
                raise ValueError("Usage: lobby create [max]")
            result = await self.client.request(
                "lobby_create",
                tournament_id=self._tournament_id(),
                max_players=maximum,
            )
        elif operation == "join":
            if not arguments:
                raise ValueError("Usage: lobby join <lobby-id|invitation-code>")
            result = await self.client.request("lobby_join", target=arguments[0])
        elif operation == "observe":
            if not 1 <= len(arguments) <= 2 or (
                len(arguments) == 2 and arguments[1].casefold() != "confirm"
            ):
                raise ValueError("Usage: lobby observe <lobby-id|invitation-code> [confirm]")
            result = await self.client.request(
                "lobby_observe",
                target=arguments[0],
                confirm_fresh=len(arguments) == 2,
            )
        elif operation == "role":
            if not 1 <= len(arguments) <= 2 or arguments[0].casefold() not in {
                "player",
                "observer",
            }:
                raise ValueError("Usage: lobby role <player|observer> [confirm]")
            if len(arguments) == 2 and arguments[1].casefold() != "confirm":
                raise ValueError("Usage: lobby role <player|observer> [confirm]")
            result = await self.client.request(
                "lobby_role",
                lobby_id=self._lobby_id([]),
                role=arguments[0].casefold(),
                confirm_fresh=len(arguments) == 2,
            )
        elif operation == "info":
            result = await self.client.request("lobby_info", lobby_id=self._lobby_id(arguments))
        elif operation == "packet":
            if len(arguments) != 2 or arguments[0].casefold() not in {"add", "remove"}:
                raise ValueError("Usage: lobby packet <add|remove> <packet-id>")
            packet_operation, packet_id = arguments[0].casefold(), arguments[1]
            result = await self.client.request(
                "lobby_packet" if packet_operation == "add" else "lobby_packet_remove",
                lobby_id=self._lobby_id([]),
                packet_id=packet_id,
            )
        elif operation == "setting":
            if len(arguments) != 2:
                raise ValueError("Usage: lobby setting <name> <value>")
            name, raw_value = arguments
            result = await self.client.request(
                "lobby_settings",
                lobby_id=self._lobby_id([]),
                changes={name: self._setting_value(name, raw_value)},
            )
        elif operation == "ready":
            if len(arguments) > 1 or (arguments and arguments[0].casefold() != "off"):
                raise ValueError("Usage: lobby ready [off]")
            ready = not arguments
            result = await self.client.request(
                "lobby_ready", lobby_id=self._lobby_id([]), ready=ready
            )
        elif operation == "suggestions":
            result = await self.client.request("lobby_suggestions", lobby_id=self._lobby_id([]))
            self._show_packets(result)
            return
        elif operation == "find":
            if len(arguments) > 1 or (arguments and arguments[0].casefold() != "cancel"):
                raise ValueError("Usage: lobby find [cancel]")
            cancel = bool(arguments)
            result = await self.client.request(
                "lobby_find_players_cancel" if cancel else "lobby_find_players",
                lobby_id=self._lobby_id([]),
            )
        elif operation == "start":
            result = await self.client.request("lobby_start", lobby_id=self._lobby_id([]))
            game = result.get("game")
            if game:
                self.current_game_id = game["id"]
                print(f"Game assigned: {self.current_game_id}. Use 'game join'.")
            self.current_tournament_id = result["lobby"]["tournament_id"]
            self._show_lobby(result["lobby"])
            return
        elif operation == "leave":
            result = await self.client.request("lobby_leave", lobby_id=self._lobby_id([]))
        elif operation == "cancel":
            result = await self.client.request("lobby_cancel", lobby_id=self._lobby_id([]))
        else:
            raise ValueError(f"Unknown lobby operation: {operation}")
        self.current_lobby_id = None if operation == "leave" else result["id"]
        self.current_tournament_id = result["tournament_id"]
        self._show_lobby(result)

    async def _blacklist(self, arguments: list[str]) -> None:
        if not arguments:
            result = await self.client.request("blacklist_list")
        elif len(arguments) == 2 and arguments[0].casefold() in {"add", "remove"}:
            result = await self.client.request(
                f"blacklist_{arguments[0].casefold()}", telegram_user_id=int(arguments[1])
            )
        else:
            raise ValueError("Usage: blacklist | blacklist <add|remove> <player-id>")
        if not result:
            print("Blacklist is empty")
            return
        for player in result:
            print(f"{player['telegram_user_id']} | {player['display_name']}")

    async def _game(self, arguments: list[str]) -> None:
        if not arguments:
            raise ValueError(
                "Usage: game <join|observe|info|buzz|answer|appeal|reputation|report|score|"
                "pause|resume|abandon|next>"
            )
        operation = arguments.pop(0).casefold()
        if operation == "observe":
            await self._observe_game(arguments)
            return
        game_id = self._game_id(arguments if operation == "info" else [])
        if operation == "info":
            result = await self.client.request("game_info", game_id=game_id)
            self._update_game_context(result)
            self._show_game(result)
            return
        if operation == "join":
            result = await self.client.request("game_join", game_id=game_id)
        elif operation == "buzz":
            result = await self.client.request("game_buzz", game_id=game_id)
        elif operation == "answer":
            if not arguments:
                raise ValueError("Usage: game answer <answer text>")
            result = await self.client.request(
                "game_answer", game_id=game_id, answer=" ".join(arguments)
            )
        elif operation == "appeal":
            result = await self._game_appeal(game_id, arguments)
        elif operation == "reputation":
            if len(arguments) != 2 or arguments[0].casefold() not in {
                "upvote",
                "downvote",
            }:
                raise ValueError("Usage: game reputation <upvote|downvote> <player-id>")
            result = await self.client.request(
                "game_reputation_vote",
                game_id=game_id,
                value=1 if arguments[0].casefold() == "upvote" else -1,
                telegram_user_id=int(arguments[1]),
            )
            self._show_json(result)
            return
        elif operation == "report":
            if len(arguments) < 2 or arguments[0].casefold() not in {
                "cheating",
                "toxicity",
            }:
                raise ValueError("Usage: game report <cheating|toxicity> <player-id> [details]")
            result = await self.client.request(
                "game_report",
                game_id=game_id,
                kind=arguments[0].casefold(),
                telegram_user_id=int(arguments[1]),
                details=" ".join(arguments[2:]) or None,
            )
            self._show_json(result)
            return
        elif operation == "pause":
            result = await self.client.request("game_pause", game_id=game_id)
        elif operation == "resume":
            result = await self.client.request("game_resume", game_id=game_id)
        elif operation == "score":
            result = await self.client.request("game_score", game_id=game_id)
        elif operation == "abandon":
            result = await self.client.request("game_abandon", game_id=game_id)
        elif operation == "next":
            result = await self.client.request("game_advance", game_id=game_id)
        else:
            raise ValueError(f"Unknown game operation: {operation}")
        self._show_transition(result)

    async def _observe_game(self, arguments: list[str]) -> None:
        if arguments and arguments[0].casefold() == "list":
            if len(arguments) != 1:
                raise ValueError("Usage: game observe list")
            games = await self.client.request(
                "game_observe_list", tournament_id=self._tournament_id()
            )
            if not games:
                print("No ongoing games to observe")
                return
            for game in games:
                availability = "available" if game["can_observe"] else "unavailable"
                warning = (
                    f", burns {game['fresh_content_count']} fresh claims; confirm required"
                    if game["confirmation_required"]
                    else ""
                )
                print(
                    f"{game['id']} | {game['status']}/{game['phase']} | "
                    f"{game['participant_count']} players | {availability}{warning}"
                )
            return
        if arguments and arguments[0].casefold() == "leave":
            if len(arguments) > 2:
                raise ValueError("Usage: game observe leave [game-id]")
            game_id = self._game_id(arguments[1:])
            result = await self.client.request("game_observe_leave", game_id=game_id)
            self._show_game(result)
            return
        if not 1 <= len(arguments) <= 2 or (
            len(arguments) == 2 and arguments[1].casefold() != "confirm"
        ):
            raise ValueError("Usage: game observe <game-id> [confirm]")
        game_id = arguments[0]
        result = await self.client.request(
            "game_observe",
            game_id=game_id,
            confirm_fresh=len(arguments) == 2,
        )
        if result["confirmation_required"]:
            print(
                f"Warning: observing would burn {result['fresh_content_count']} fresh "
                f"content claims. Repeat: game observe {game_id} confirm"
            )
            return
        self.current_game_id = game_id
        self._update_game_context(result["snapshot"])
        for event in result.get("events", []):
            self._show_game_event(event)
        self._show_game(result["snapshot"])

    async def _game_appeal(self, game_id: str, arguments: list[str]) -> dict[str, Any]:
        if not arguments:
            return await self.client.request("game_appeal", game_id=game_id)
        operation = arguments.pop(0).casefold()
        if operation == "submit":
            if len(arguments) > 1:
                raise ValueError("Usage: game appeal submit [attempt-id]")
            return await self.client.request(
                "game_appeal",
                game_id=game_id,
                target_attempt_id=arguments[0] if arguments else None,
            )
        if operation == "vote":
            if len(arguments) != 1 or arguments[0].casefold() not in {"approve", "reject"}:
                raise ValueError("Usage: game appeal vote <approve|reject>")
            return await self.client.request(
                "game_appeal_vote",
                game_id=game_id,
                appeal_id=self._appeal_id(),
                approve=arguments[0].casefold() == "approve",
            )
        if operation == "escalate":
            if len(arguments) != 1:
                raise ValueError("Usage: game appeal escalate <on|off>")
            return await self.client.request(
                "game_appeal_escalate",
                game_id=game_id,
                appeal_id=self._appeal_id(),
                escalate=self._boolean_value(arguments[0]),
            )
        if operation in {"comment", "commentary"}:
            if not arguments:
                raise ValueError("Usage: game appeal comment <text>")
            return await self.client.request(
                "game_appeal_commentary",
                game_id=game_id,
                appeal_id=self._appeal_id(),
                commentary=" ".join(arguments),
            )
        if arguments:
            raise ValueError("Usage: game appeal [submit [attempt-id]|vote|escalate|comment]")
        return await self.client.request(
            "game_appeal", game_id=game_id, target_attempt_id=operation
        )

    async def _appeals(self, arguments: list[str]) -> None:
        if arguments and arguments[0].casefold() == "decide":
            if len(arguments) != 3 or arguments[2].casefold() not in {"approve", "reject"}:
                raise ValueError("Usage: appeals decide <appeal-id> <approve|reject>")
            result = await self.client.request(
                "manager_appeal_decide",
                appeal_id=arguments[1],
                approve=arguments[2].casefold() == "approve",
            )
            outcome = "approved" if result["approved"] else "rejected"
            print(f"Appeal {result['appeal_id']} {outcome} ({result['source']})")
            return
        if len(arguments) > 1 or (arguments and arguments[0].casefold() != "all"):
            raise ValueError("Usage: appeals [all] | appeals decide <appeal-id> <approve|reject>")
        tournament_id = None if arguments else self._tournament_id()
        tickets = await self.client.request("manager_appeal_tickets", tournament_id=tournament_id)
        self._show_appeal_tickets(tickets)

    async def _packet_admin(self, arguments: list[str]) -> None:
        if len(arguments) < 2:
            raise ValueError("Usage: packet <import|preview|publish|reject|release> <target> ...")
        operation, target = arguments[0].casefold(), arguments[1]
        if operation == "import":
            path = Path(target)
            result = await self.client.request(
                "packet_import",
                source_filename=path.name,
                source_base64=base64.b64encode(path.read_bytes()).decode("ascii"),
                tournament_id=self._tournament_id(),
            )
        elif operation == "preview":
            result = await self.client.request("packet_preview", draft_id=target)
        elif operation == "publish":
            result = await self.client.request("packet_publish", draft_id=target)
        elif operation == "reject":
            result = await self.client.request("packet_reject", draft_id=target)
        elif operation == "release":
            if len(arguments) not in {3, 4}:
                raise ValueError("Usage: packet release <packet-id> <on|off> [packet-version-id]")
            result = await self.client.request(
                "packet_library_release_set",
                packet_id=target,
                released=self._boolean_value(arguments[2]),
                packet_version_id=arguments[3] if len(arguments) == 4 else None,
            )
        else:
            raise ValueError(f"Unknown packet operation: {operation}")
        self._show_json(result)

    async def _admin(self, arguments: list[str]) -> None:
        if not arguments:
            raise ValueError("Usage: admin <token|suspicion> ...")
        operation = arguments.pop(0).casefold()
        if operation == "token" and arguments:
            token_operation = arguments.pop(0).casefold()
            if token_operation == "issue" and len(arguments) <= 1:
                result = await self.client.request(
                    "admin_tournament_token_issue",
                    intended_creator_id=arguments[0] if arguments else None,
                )
            elif token_operation == "revoke" and len(arguments) == 1:
                result = await self.client.request(
                    "admin_tournament_token_revoke", token=arguments[0]
                )
            else:
                raise ValueError("Usage: admin token <issue [creator-uuid]|revoke <token>>")
        elif operation == "suspicion" and arguments:
            suspicion_operation = arguments.pop(0).casefold()
            if suspicion_operation == "review" and len(arguments) in {1, 2}:
                result = await self.client.request(
                    "admin_suspicion_review",
                    player_id=arguments[0],
                    limit=int(arguments[1]) if len(arguments) == 2 else 20,
                )
            elif suspicion_operation == "clear" and len(arguments) >= 2:
                result = await self.client.request(
                    "admin_suspicion_clear",
                    player_id=arguments[0],
                    note=" ".join(arguments[1:]),
                )
            else:
                raise ValueError(
                    "Usage: admin suspicion <review <player-uuid> [limit]|"
                    "clear <player-uuid> <note>>"
                )
        else:
            raise ValueError("Usage: admin <token|suspicion> ...")
        self._show_json(result)

    def _update_context(self, result: dict[str, Any]) -> None:
        self.current_lobby_id = result.get("lobby_id") or self.current_lobby_id
        self.current_game_id = (
            result.get("game_id")
            or result.get("reconnectable_game_id")
            or result.get("last_game_id")
        )

    def _tournament_id(self) -> str:
        if self.current_tournament_id is None:
            raise ValueError(
                "No current tournament; use 'tournament list mine' then 'tournament use'"
            )
        return self.current_tournament_id

    def _lobby_id(self, arguments: list[str]) -> str:
        if arguments:
            self.current_lobby_id = arguments[0]
        if self.current_lobby_id is None:
            raise ValueError("No current lobby; supply a lobby ID or join/create one")
        return self.current_lobby_id

    def _game_id(self, arguments: list[str]) -> str:
        if arguments:
            self.current_game_id = arguments[0]
        if self.current_game_id is None:
            raise ValueError("No current game; supply a game ID or start one")
        return self.current_game_id

    def _appeal_id(self) -> str:
        if self.current_appeal_id is None:
            raise ValueError("No current appeal; use 'game info' to refresh appeal state")
        return self.current_appeal_id

    def _update_game_context(self, game: dict[str, Any]) -> None:
        self.current_tournament_id = game["tournament_id"]
        appeal = game.get("appeal")
        self.current_appeal_id = appeal["id"] if appeal else None

    @staticmethod
    def _show_lobby(lobby: dict[str, Any]) -> None:
        print(
            f"Lobby {lobby['id']} ({lobby['status']}) | tournament={lobby['tournament_id']} "
            f"| invite={lobby['invitation_code']} | capacity={lobby['max_players']}"
        )
        if not lobby.get("hybrid_matchmaking_available", False):
            print("Find players: unavailable for this tournament")
        elif lobby.get("searching"):
            print(
                "Find players: active | other searching "
                f"lobbies={lobby.get('other_searching_lobby_count', 0)}, "
                f"players={lobby.get('other_searching_player_count', 0)}"
            )
        elif lobby.get("merged_into_lobby_id"):
            print(f"Merged into lobby {lobby['merged_into_lobby_id']}")
        else:
            print(
                "Find players: inactive | other searching "
                f"lobbies={lobby.get('other_searching_lobby_count', 0)}, "
                f"players={lobby.get('other_searching_player_count', 0)}"
            )
        packets = lobby.get("selected_packets", [])
        if packets:
            print("Selected packets:")
            for packet in packets:
                print(
                    f"  {packet['name']} ({packet['fresh_play_unit_count']}/"
                    f"{packet['total_play_unit_count']} common fresh play units)"
                )
        else:
            print("Selected packets: none")
        print(
            f"Play units requested: {lobby.get('settings', {}).get('theme_count', '-')}; "
            f"available fresh: {lobby.get('available_play_unit_count', '-')}; "
            f"violations: {lobby.get('validation_violations') or '-'}"
        )
        settings = lobby.get("settings", {})
        if settings:
            speed = settings.get("question_token_delay")
            speed_text = "instantaneous" if speed == 0 else f"{speed:g}s/token"
            print(
                "Settings: "
                f"messages={settings.get('message_delay'):g}s, question={speed_text}, "
                f"token target={settings.get('question_token_target_chars')} chars, "
                f"pausing={settings.get('pausing_allowed')}"
            )
            print(
                f"  values={settings.get('question_values')}, "
                f"minus multiplier={settings.get('minus_multiplier')}, "
                f"themes={settings.get('theme_count')}"
            )
            print(
                "  game→theme={game_start_to_first_theme_delay:g}s, "
                "theme→question={theme_to_first_question_delay:g}s, "
                "cost→text={question_cost_announcement_delay:g}s, "
                "text→buzz timer={buzz_timer_countdown_delay:g}s".format(**settings)
            )
            print(
                "  questions={between_questions_delay:g}s, "
                "last question→complete={last_question_to_theme_complete_delay:g}s, "
                "complete→scores={theme_complete_to_scoreboard_delay:g}s, "
                "scores→theme={between_themes_delay:g}s".format(**settings)
            )
        if not lobby.get("members"):
            print("Members: none (the lobby may already have been consumed)")
        for member in lobby.get("members", []):
            violations = member.get("validation_violations") or []
            role = member.get("role", "player")
            print(
                f"  {member['join_order']}. {member['display_name']} "
                f"(id={member['telegram_user_id']}, role={role}, "
                f"ready={member['ready'] if role == 'player' else '-'}, "
                f"violations={violations or '-'})"
            )

    @staticmethod
    def _show_game(game: dict[str, Any]) -> None:
        print(
            f"\nGame {game['id']} | tournament={game['tournament_id']} | "
            f"ruleset={game['game_ruleset']} v{game['game_ruleset_version']} | "
            f"{game['status']}/{game['phase']} | version {game['version']}"
        )
        if game["status"] == "lobby":
            print(
                "Assigned: "
                + ", ".join(
                    f"{player['display_name']} (joined={player['joined']})"
                    for player in game["participants"]
                )
            )
        question = game.get("question")
        if question and game["phase"] == "question":
            if question.get("accepted_buzzer_id") is None:
                if question["text"]:
                    print(
                        f"Question {question['sequence']} for {question['value']}: "
                        f"{question['text']}"
                    )
                else:
                    print(f"Question for {question['value']}: waiting for text")
            else:
                print(f"Player {question['accepted_buzzer_id']} buzzed; form: {question['form']}")
        if question and question.get("revealed_answer") is not None:
            print(f"Answer: {question['revealed_answer']}")
            if question.get("commentary"):
                print(f"Commentary: {question['commentary']}")
            attempts = question.get("attempts", [])
            if attempts:
                print("Attempts (use the attempt ID when an appeal has multiple targets):")
                for attempt in attempts:
                    answer = "[timeout]" if attempt["timed_out"] else attempt["submitted_answer"]
                    judgment = "correct" if attempt["final_correct"] else "incorrect"
                    changed = (
                        " (changed by appeal)"
                        if attempt["final_correct"] != attempt["original_correct"]
                        else ""
                    )
                    print(
                        f"  #{attempt['attempt_number']} player {attempt['player_id']}: "
                        f"{answer!r} — {judgment}{changed}; id={attempt['id']}"
                    )
        appeal = game.get("appeal")
        if appeal:
            InteractiveConsole._show_appeal(appeal)
        if game.get("paused"):
            print("Game paused")
        if game.get("join_deadline"):
            print(f"Player join deadline: {game['join_deadline']}")
        if game.get("pause_abandonment_deadline"):
            print(f"Paused game abandonment deadline: {game['pause_abandonment_deadline']}")
        for player in game["participants"]:
            if player.get("abandoned_at") and not player.get("active"):
                suffix = " (may reconnect)" if player.get("can_reconnect") else ""
                print(f"{player['display_name']} abandoned{suffix}")
        active_observers = [item for item in game.get("observers", []) if item.get("active")]
        if active_observers:
            print(
                "Observers: " + ", ".join(observer["display_name"] for observer in active_observers)
            )
        if game["phase"] == "countdown":
            print("Waiting for the pre-start countdown")
        if game["status"] in {"completed", "finalized"}:
            print("Results:")
            for player in sorted(game["participants"], key=lambda item: item["place"] or 99):
                print(
                    f"  {player['place']}. {player['display_name']}: {player['score']} "
                    f"(rating {player['rating']})"
                )
        elif game.get("rating_pending"):
            print("Results recorded; rating is waiting for earlier rating results")

    def _show_transition(self, result: dict[str, Any]) -> None:
        for event in result.get("events", []):
            self._show_game_event(event)
        if not result.get("accepted"):
            print("Command had no effect")
        self._update_game_context(result["snapshot"])
        self._show_game(result["snapshot"])

    @staticmethod
    def _show_appeal(appeal: dict[str, Any]) -> None:
        print(
            f"Appeal {appeal['id']} | {appeal['kind']} | status={appeal['status']} | "
            f"answer={appeal['submitted_answer']!r}"
        )
        if appeal["status"] == "voting":
            print(
                f"  votes: {appeal['approvals']} approve, {appeal['rejections']} reject, "
                f"electorate {appeal['electorate_size']}; deadline {appeal['vote_deadline']}"
            )
            print("  Vote with: game appeal vote <approve|reject>")
        elif appeal["status"] == "awaiting_escalation":
            print(
                f"  Appellant must choose by {appeal['escalation_deadline']}: "
                "game appeal escalate <on|off>"
            )
        elif appeal["status"] == "awaiting_commentary":
            print(
                f"  Appellant commentary due {appeal['commentary_deadline']}: "
                "game appeal comment <text>"
            )
        elif appeal["status"] == "escalated":
            print(f"  Anonymous manager review expires {appeal['ticket_expires_at']}")

    @staticmethod
    def _show_game_event(event: dict[str, Any]) -> None:
        kind = event["kind"]
        payload = event.get("payload", {})
        if kind == "ready_countdown":
            print(f"\nGame starts in {payload['seconds']:g} seconds")
        elif kind == "themes_announced":
            print("\nThemes in play:")
            for index, theme in enumerate(payload["themes"]):
                if index == 0 or theme.get("packet_changed"):
                    print(f"  From packet: {theme['packet_name']}")
                author = f" — {theme['author']}" if theme.get("author") else ""
                print(f"  {theme['position']}. {theme['name']}{author}")
        elif kind == "theme_started":
            if payload.get("packet_changed"):
                print(f"\nPacket changed: {payload['packet_name']}")
            print(f"\n{payload['name']}")
            if payload.get("author"):
                print(f"Author: {payload['author']}")
        elif kind == "theme_completed":
            print(f"\nTheme completed: {payload['name']}")
        elif kind == "player_abandoned":
            print(f"\nPlayer abandoned: {payload['participant_id']}")
        elif kind == "player_reconnected":
            print(f"\nPlayer reconnected: {payload['participant_id']}")
        elif kind == "game_cancelled":
            if payload.get("reason") in {"no_players_joined", "not_all_players_joined"}:
                print("\nGame failed to start before the join deadline; no content was burnt")
            else:
                print("\nGame cancelled before theme reveal; no themes were burnt")
        elif kind == "pause_abandonment_deadline_started":
            print(f"\nGame will be abandoned if nobody reconnects by {payload['deadline']}")
        elif kind == "pause_abandonment_deadline_cancelled":
            print("\nPaused-game abandonment timer cancelled")
        elif kind == "game_abandoned" and payload.get("reason") == "pause_inactivity_timeout":
            print("\nPaused game abandoned after all players remained disconnected")
        elif kind == "rating_deferred":
            print("\nRating deferred until earlier rating results settle")
        elif kind == "finalization_deferred":
            print("\nFinalization deferred until escalated appeals are resolved")
        elif kind == "appeal_voting_started":
            print(
                f"\nAppeal voting started for {payload['submitted_answer']!r} "
                f"({payload['kind']}); deadline {payload['vote_deadline']}"
            )
        elif kind == "appeal_vote_cast":
            print(
                f"\nAppeal vote recorded ({payload['votes_cast']}/"
                f"{payload['electorate_size']} cast)"
            )
        elif kind == "appeal_vote_rejected":
            print(
                f"\nAppeal vote rejected; appellant may escalate by "
                f"{payload['escalation_deadline']}"
            )
        elif kind == "appeal_commentary_requested":
            print(f"\nAppeal commentary requested by {payload['deadline']}")
        elif kind == "appeal_escalated":
            print(f"\nAppeal sent anonymously to managers; expires {payload['ticket_expires_at']}")
        elif kind == "appeal_resolved":
            outcome = "approved" if payload["approved"] else "rejected"
            print(f"\nAppeal {outcome} ({payload['source']})")
        elif kind == "appeal_score_corrected":
            print("\nAppeal score corrections:")
            for correction in payload["corrections"]:
                print(f"  player {correction['participant_id']}: {correction['delta']:+g}")
        elif kind == "question_cost_announced":
            print(f"\nQuestion for {payload['value']}:")
        elif kind == "question_token_revealed":
            print(f"\nQuestion: {payload['text']}")
        elif kind == "question_fully_announced":
            delay = payload["buzz_timer_countdown_delay"]
            print(f"\nQuestion fully announced; buzz timer starts in {delay:g}s")
        elif kind == "buzz_timer_started":
            print(f"\nBuzz timer started: {payload['seconds']:g}s")
        elif kind == "scoreboard":
            print("\nScores:")
            for player in payload["players"]:
                print(f"  {player['display_name']}: {player['score']}")
        else:
            print(f"\n[{event['sequence']}] {kind}: {payload}")

    @staticmethod
    def _show_packets(packets: list[dict[str, Any]]) -> None:
        if not packets:
            print("No packets available")
        for packet in packets:
            rights = packet.get("access")
            access = (
                [
                    f"level={rights['level']}" if name == "level" else name
                    for name, granted in rights.items()
                    if granted
                ]
                if isinstance(rights, dict)
                else ["playable"]
            )
            print(
                f"{packet['packet_id']} | {packet['name']}"
                f" ({packet.get('year') or 'year unknown'}, "
                f"lead: {packet.get('lead_author') or '-'}, "
                f"language: {packet.get('language', 'und')}) | "
                f"access: {', '.join(access) or 'metadata'} "
                f"| fresh play units {packet.get('fresh_play_unit_count', '-')}/"
                f"{packet.get('total_play_unit_count', '-')} | version {packet.get('version', '-')}"
            )

    @staticmethod
    def _show_tournament_listing(
        listing: dict[str, list[dict[str, Any]]], *, empty_message: str
    ) -> None:
        populated = False
        for phase, heading in (
            ("ongoing", "Ongoing"),
            ("future", "Future"),
            ("past", "Past"),
        ):
            tournaments = listing.get(phase, [])
            if not tournaments:
                continue
            populated = True
            print(f"{heading} tournaments:")
            for tournament in tournaments:
                membership = tournament.get("membership_status")
                access = (
                    f"membership={membership}"
                    if membership is not None
                    else f"joinable={'yes' if tournament['joinable'] else 'no'}"
                )
                starts_at = tournament.get("starts_at") or "unscheduled"
                ends_at = (
                    tournament.get("planned_ends_at") or "open-ended"
                )
                print(
                    f"  {tournament['id']} | {tournament['name']} ({tournament['slug']}) | "
                    f"{tournament['type_key']} | {tournament['ruleset_key']} "
                    f"v{tournament['ruleset_version']} | {tournament['visibility']} | {access}"
                )
                print(f"    schedule: {starts_at} → {ends_at}; status={tournament['status']}")
                authors = tournament.get("authors", [])
                if authors:
                    print(f"    authors: {', '.join(authors)}")
        if not populated:
            print(empty_message)

    @staticmethod
    def _show_tournament(tournament: dict[str, Any]) -> None:
        role = "manager" if tournament["manager"] else "player"
        print(
            f"Tournament {tournament['name']} ({tournament['slug']}) | {tournament['id']}\n"
            f"  status={tournament['status']}, visibility={tournament['visibility']}, "
            f"role={role}, tournament rating={tournament['rating']}, "
            f"ruleset rating={tournament.get('ruleset_rating', 1000)}\n"
            f"  confidence: tournament={tournament.get('rating_confidence', 0)}, "
            f"ruleset={tournament.get('ruleset_rating_confidence', 0)}, "
            f"model={tournament.get('rating_confidence_model', 'time_weighted')}\n"
            f"  registration={'open' if tournament.get('registration_open') else 'closed'}, "
            f"language={tournament.get('language', 'und')}, "
            f"payment={tournament.get('payment_type', 'free')}\n"
            f"  schedule={tournament.get('starts_at') or 'unscheduled'} → "
            f"{tournament.get('planned_ends_at') or 'open-ended'}\n"
            f"  type={tournament['type']} v{tournament['type_version']}, "
            f"ruleset={tournament['game_ruleset']} v{tournament['game_ruleset_version']}, "
            f"policy v{tournament['policy_version']}"
        )
        print(
            "  player-mutable settings: "
            + (", ".join(tournament["player_mutable_parameters"]) or "none")
        )
        if tournament.get("authors"):
            print("  authors: " + ", ".join(tournament["authors"]))
        if tournament.get("pricing_plans"):
            print("  pricing plans:")
            for plan in tournament["pricing_plans"]:
                prices = "/".join(
                    f"{price['amount']} {price['currency']}" for price in plan["prices"]
                )
                print(f"    {plan['name']}: {prices}")
        if tournament.get("registration_rejection_reasons"):
            print("  registration declined:")
            for reason in tournament["registration_rejection_reasons"]:
                print(f"    - {reason}")
        if tournament.get("type_supports_hybrid_matchmaking"):
            state = "enabled" if tournament.get("hybrid_matchmaking_enabled") else "disabled"
            print(f"  hybrid matchmaking: {state} by tournament policy")
        else:
            print("  hybrid matchmaking: unsupported by tournament type")
        print("  defaults:")
        print(json.dumps(tournament["default_parameters"], ensure_ascii=False, indent=2))
        print("  policies:")
        print(json.dumps(tournament["policies"], ensure_ascii=False, indent=2))

    @staticmethod
    def _show_appeal_tickets(tickets: list[dict[str, Any]]) -> None:
        if not tickets:
            print("No open appeal tickets")
            return
        print("Anonymous appeal tickets:")
        for ticket in tickets:
            print(
                f"  {ticket['id']} | question {ticket['question_sequence']} | "
                f"{ticket['kind']} | expires {ticket['expires_at']}"
            )
            print(f"    Question: {ticket['question_text']}")
            print(f"    Official answer: {ticket['official_answer']}")
            print(f"    Appealed answer: {ticket['submitted_answer']}")
            print(f"    Commentary: {ticket['commentary'] or '-'}")

    @staticmethod
    def _show_json(value: Any) -> None:
        print(json.dumps(value, ensure_ascii=False, indent=2))

    @staticmethod
    def _setting_value(name: str, value: str) -> object:
        if name == "pausing_allowed":
            return InteractiveConsole._boolean_value(value)
        if name in {"question_token_target_chars", "theme_count"}:
            return int(value)
        if name == "question_values":
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as error:
                raise ValueError("question_values must be a JSON list of integers") from error
            if not isinstance(parsed, list):
                raise ValueError("question_values must be a JSON list of integers")
            return parsed
        if name == "question_token_delay" and value.casefold() in {
            "instant",
            "instantaneous",
        }:
            return 0.0
        return float(value)

    @staticmethod
    def _boolean_value(value: str) -> bool:
        normalized = value.casefold()
        if normalized in {"true", "yes", "on"}:
            return True
        if normalized in {"false", "no", "off"}:
            return False
        raise ValueError("Value must be on or off")

    def _require_login(self) -> None:
        if not self.logged_in:
            raise ValueError("Log in first")

    @staticmethod
    def _help() -> None:
        print(
            "Commands:\n"
            '  login <player-id> "<display-name>" [--admin [token]]\n'
            "  me | tournament list <mine|public>\n"
            "  tournament use <tournament-id> | tournament info\n"
            "  tournament manage <tournament-id>\n"
            '  tournament create <token> <slug> "<name>" [creation options]\n'
            "    --type <type> --ruleset <ruleset> --visibility <public|private>\n"
            "    --starts-at <ISO-8601> --planned-ends-at <ISO-8601>\n"
            "  tournament member add <player-uuid>\n"
            "  tournament registrations | tournament approve <player-uuid|all>\n"
            "  tournament finalize (finish setup)\n"
            "  tournament start | tournament stage start <first|playoff>\n"
            "  tournament participants finalize [approved-player-uuid ...]\n"
            "  tournament manager <add|remove> <player-uuid>\n"
            "  tournament setting <name> <value> | tournament mutable <name> <on|off>\n"
            "  tournament policy <name> <json-value>\n"
            "  tournament requirement <list|add|remove> ...\n"
            "  tournament packet assign <packet-id> [--version <version-id>] [access flags]\n"
            "  tournament packet entitlement <assignment-id> <player-uuid> <right> <on|off>\n"
            "  packets | lobby create [max]\n"
            "  lobby join <lobby-id|invitation-code> | lobby info [lobby-id]\n"
            "  lobby observe <lobby-id|invitation-code> [confirm]\n"
            "  lobby role <player|observer> [confirm]\n"
            "  lobby packet <add|remove> <packet-id>\n"
            "  lobby suggestions\n"
            "  lobby find | lobby find cancel\n"
            "  lobby ready [off] | lobby setting <name> <value>\n"
            "  lobby start | lobby leave | lobby cancel\n"
            "  game join | game info [game-id] | game buzz | game answer <text>\n"
            "  game observe list | game observe <game-id> [confirm] | game observe leave\n"
            "  game appeal [attempt-id] | game appeal vote <approve|reject>\n"
            "  game appeal escalate <on|off> | game appeal comment <text>\n"
            "  game reputation <upvote|downvote> <player-id>\n"
            "  game report <cheating|toxicity> <player-id> [details]\n"
            "  game score | game pause | game resume | game abandon | game next\n"
            "  appeals [all] | appeals decide <appeal-id> <approve|reject>\n"
            "  packet import <json-or-docx> | packet preview <draft-id>\n"
            "  packet publish <draft-id> | packet reject <draft-id>\n"
            "  packet release <packet-id> <on|off> [packet-version-id]\n"
            "  admin token issue [creator-uuid] | admin token revoke <token>\n"
            "  admin suspicion review <player-uuid> [limit]\n"
            "  admin suspicion clear <player-uuid> <note>\n"
            "  blacklist | blacklist <add|remove> <player-id>\n"
            "  help | quit"
        )


async def main(args: argparse.Namespace) -> None:
    console: InteractiveConsole

    async def event_handler(message: dict[str, Any]) -> None:
        await console.event(message)

    client = ConsoleProtocolClient(args.host, args.port, event_handler=event_handler)
    console = InteractiveConsole(client)
    await client.connect()
    try:
        if args.player_id is not None:
            result = await client.request(
                "login",
                telegram_user_id=args.player_id,
                display_name=args.name or f"Player {args.player_id}",
                admin=args.admin,
                admin_token=args.admin_token,
            )
            console.logged_in = True
            console._update_context(result)
            console._show_json(result)
        await console.run()
    finally:
        await client.close()


def run() -> None:
    parser = argparse.ArgumentParser(description="Connect to the SITG localhost server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--player-id", type=int)
    parser.add_argument("--name")
    parser.add_argument("--admin", action="store_true")
    parser.add_argument("--admin-token")
    try:
        asyncio.run(main(parser.parse_args()))
    except (ConnectionError, KeyboardInterrupt) as error:
        if not isinstance(error, KeyboardInterrupt):
            raise SystemExit(f"Cannot connect to server: {error}") from error


if __name__ == "__main__":
    run()
