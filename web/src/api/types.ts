import type { Locale } from "../i18n";

export type StableErrorCode =
  | "authentication_required"
  | "forbidden"
  | "not_found"
  | "validation_failed"
  | "stale_write"
  | "idempotency_key_required"
  | "idempotency_conflict"
  | "request_in_progress"
  | "request_indeterminate"
  | "capability_unavailable"
  | "secret_already_delivered"
  | "internal_error"
  | "network"
  | "invalid_response";

export interface ApiErrorPayload {
  code: StableErrorCode;
  message_key?: string;
  retryable?: boolean;
  correlation_id?: string;
}

export interface SessionPayload {
  csrf_token: string;
  expires_at: string;
  locale: Locale;
}

export interface RouteAuthorization {
  allowed: boolean;
  reason_code?: StableErrorCode;
}

export interface RouteResource {
  state: "ready" | "empty";
  summary?: string;
  items?: Array<{ id: string; label: string; description?: string }>;
}

export type TournamentAction = "info" | "register" | "select_player" | "select_manager";

export interface TournamentPrice {
  amount: number;
  currency: string;
}

export interface TournamentPricingPlan {
  id: string;
  name: string;
  prices: TournamentPrice[];
}

export interface TournamentListItem {
  id: string;
  name: string;
  slug: string;
  description?: string;
  status: string;
  phase: "future" | "ongoing" | "past";
  visibility: "public" | "private";
  starts_at?: string | null;
  planned_ends_at?: string | null;
  actual_ends_at?: string | null;
  actual_starts_at?: string | null;
  language: string;
  payment_type: string;
  pricing_plans: TournamentPricingPlan[];
  registration_open: boolean;
  registration_starts_at?: string | null;
  registration_ends_at?: string | null;
  authors: string[];
  type_key: string;
  type_version: number;
  ruleset_key: string;
  ruleset_version: number;
  membership_status?: string | null;
  managed: boolean;
  policy_version: number;
  available_actions: TournamentAction[];
  finalized_at?: string | null;
  settings_version?: number;
}

export interface TournamentRouteResource {
  kind: "tournaments";
  state: "ready" | "empty";
  role: "player" | "manager" | "admin";
  navigation_version: number;
  total: number;
  items: TournamentListItem[];
}

export interface TournamentRequirement {
  id: string;
  kind: string;
  target_id: string;
  target_name: string;
  failure_message?: string | null;
}

export interface TournamentDetailsPayload {
  tournament: TournamentListItem;
  registration_requirements: TournamentRequirement[];
  policies: Record<string, unknown>;
  default_parameters: Record<string, unknown>;
  player_mutable_parameters: string[];
}

export type TournamentProfileStatus =
  | "invited"
  | "registered"
  | "approved"
  | "active"
  | "rejected"
  | (string & {});

export interface TournamentProfileRegistration {
  player_id: string;
  nickname: string;
  status: TournamentProfileStatus;
  registered_at?: string | null;
}

export interface TournamentProfileGameParticipant {
  player_id: string;
  nickname: string;
  score: number | string;
  place: number | string | null;
}

export interface TournamentProfileGame {
  game_id: string;
  played_at?: string | null;
  participants: TournamentProfileGameParticipant[];
}

export interface TournamentProfileManualResult {
  player_id: string;
  nickname: string;
  place: string;
  score: string;
  points: string;
}

export interface TournamentProfileMatch {
  id: string;
  group: number;
  number: number;
  game_id: string | null;
  played_at?: string | null;
  participants: TournamentProfileGameParticipant[];
  players: Array<{ player_id: string; nickname: string }>;
  manual_results: TournamentProfileManualResult[];
}

export interface TournamentProfileStage {
  kind: "first" | "playoff";
  stage_type: "none" | "groups" | "quiz" | "swiss" | "playoff";
  scheme_key: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  groups: number[];
  rounds: Array<{ number: number; matches: TournamentProfileMatch[] }>;
}

export interface TournamentProfileLeadersStage {
  kind: "first" | "playoff";
  stage_type: "none" | "groups" | "quiz" | "swiss" | "playoff";
  standings?: Array<{
    player_id: string;
    nickname: string;
    points: string;
    score: string;
    opponent_place_sum?: string;
  }>;
  places?: Array<{ player_id: string; nickname: string; place: string }>;
}

export interface TournamentProfileResource {
  kind: "tournament_profile";
  state: "ready";
  type_key: string;
  tournament: { id: string; name: string; slug: string };
  general: {
    name: string;
    slug: string;
    description: string;
    organizer_contacts?: string;
    channel?: string;
    status: string;
    moderation_status: string;
    visibility: "public" | "private";
    language: string;
    payment_type: string;
    type: { key: string; name: string; version: number } | null;
    ruleset: { key: string; name: string; version: number } | null;
    starts_at?: string | null;
    planned_ends_at?: string | null;
    actual_starts_at?: string | null;
    actual_ends_at?: string | null;
    finalized_at?: string | null;
    settings_version: number;
    registration: { open: boolean; starts_at?: string | null; ends_at?: string | null };
    registration_link?: { reference: string; url?: string; visibility: string } | null;
    managers: Array<{ player_id: string; name: string }>;
    authors: string[];
    registration_count: number;
    participant_count: number;
  };
  registrations: TournamentProfileRegistration[];
  participants: Array<{ player_id: string; nickname: string }> | null;
  games:
    | { kind: "ladder"; items: TournamentProfileGame[] }
    | { kind: "classic"; stages: TournamentProfileStage[] };
  leaders:
    | { kind: "ladder"; items: Array<{ player_id: string; nickname: string; rating: number }> }
    | { kind: "classic"; stages: TournamentProfileLeadersStage[] };
}

export interface TournamentManagerSettingsResource {
  classic?: ClassicTournament | null;
  kind: "manager_settings";
  state: "ready";
  tournament: TournamentListItem;
  settings_version: number;
  finalized_at?: string | null;
  registration_enabled: boolean;
  ignore_late_registrations: boolean;
  organizer_contacts?: string;
  channel?: string;
  available_actions: string[];
  type_options: string[];
  ruleset_options: string[];
  policies: Record<string, unknown>;
  default_parameters: Record<string, unknown>;
  player_mutable_parameters: string[];
  author_names: string[];
  authors: Array<{ id: string; display_name: string }>;
  setting_descriptors: ManagerSettingDescriptor[];
  policy_descriptors: ManagerSettingDescriptor[];
  registration_requirements: TournamentRequirement[];
  packet_assignment_count: number;
  membership_count: number;
  manager_count: number;
}

export type ManagementSection =
  | "subscriptions"
  | "first_stage"
  | "playoff_stage"
  | "first_round_seeding"
  | "general"
  | "registrations"
  | "packet_accessibility"
  | "packet_management";

export interface ManagementRegistration {
  player_id: string;
  display_name: string;
  real_name?: string | null;
  status: string;
  registered_at: string;
  available_actions: Array<"approve" | "reject">;
}

export interface ManagementPacketPlayerAccess {
  player_id: string;
  display_name: string;
  playable: boolean;
  discoverable: boolean;
  readable: boolean;
}

export interface ManagementPacket {
  library_viewing_rule?: "never" | "after-play" | "anytime";
  default_access?: Record<"playable" | "discoverable" | "readable", boolean> | null;
  assignment_id: string;
  packet_id: string;
  packet_version_id?: string | null;
  name: string;
  version?: number | null;
  player_access: ManagementPacketPlayerAccess[];
  year?: number | null;
  published_at?: string | null;
  lead_author?: string | null;
  authors?: string[];
  released?: boolean;
}

export interface TournamentSubscriptions {
  cards: Array<{ id: string; name: string; packet_count: number | null;
    discoverable: boolean | null; readable: boolean | null; playable: boolean | null }>;
  players: Array<{ id: string; name: string; active: boolean }>;
  instances: Array<{ id: string; card_id: string; player_id: string;
    remaining_packets: number | null; revoked_at: string | null; assigned_at: string }>;
}

export interface TournamentManagerManagementResource {
  subscriptions?: TournamentSubscriptions | null;
  classic?: ClassicTournament | null;
  kind: "manager_management";
  state: "ready";
  tournament: TournamentListItem;
  sections: ManagementSection[];
  settings_version: number;
  finalized_at?: string | null;
  registration_scheduled_open: boolean;
  registration_open: boolean;
  registration_open_override?: boolean | null;
  registration_count: number;
  approved_count: number;
  participant_count: number;
  packet_count: number;
  registrations: ManagementRegistration[];
  packets: ManagementPacket[];
  available_actions: string[];
}

export interface ClassicStage {
  kind: "first" | "playoff";
  stage_type: "none" | "groups" | "quiz" | "swiss" | "playoff";
  scheme_key: string | null;
  round_count?: number | null;
  players_per_game?: number | null;
  started_at: string | null;
  completed_at: string | null;
  seeds: Array<Array<string | null>>;
  place_points: string[];
  score_multiplier: string;
  standings: Array<{ seat: string; name: string; points: string; score: string; opponent_place_sum?: string }>;
  rounds: Array<{
    id: string; number: number; assignment_id: string | null;
    discoverable: boolean; playable: boolean; start_deadline: string | null;
    packet_locked: boolean;
    matches: Array<{ id: string; group: number; number: number; players: string[];
      results: Array<{ seat: string; place: string; score: string }> | null;
      randomized: boolean; game_id: string | null }>;
  }>;
}

export interface ClassicTournament {
  schemes: Array<{ id: string; kind: string; size: number; round_count: number; opening_games?: number[][] }>;
  players: Array<{ id: string; name: string }>;
  stages: ClassicStage[];
}

export interface ManagerSettingDescriptor {
  name: string;
  value_type: "number" | "integer" | "boolean" | "array" | "enum" | "string";
  description_key: string;
  value: unknown;
  options: string[];
}

export interface AuthorSearchResource {
  items: Array<{
    author_id: string;
    display_name: string;
  }>;
  next_cursor?: string | null;
}

export interface LobbyMember {
  telegram_user_id?: number | null;
  display_name: string;
  join_order: number;
  ready: boolean;
  role: "player" | "observer";
  fresh_content_confirmed: boolean;
  validation_violations: Array<{ code: string; details?: Record<string, unknown> }>;
}

export interface LobbyPacket {
  packet_id: string;
  packet_version_id: string;
  name: string;
  year: number | null;
  published_at: string | null;
  lead_author: string | null;
  authors: string[];
  playable_for_all: boolean;
  blocked: boolean;
  total_play_unit_count: number;
  fresh_play_unit_count: number;
  validation_violations: Array<{ code: string; details?: Record<string, unknown> }>;
}

export interface LobbyResource {
  kind: "lobby";
  tournament_name?: string;
  invitation_url?: string | null;
  setting_descriptors?: ManagerSettingDescriptor[];
  state: "ready" | "empty";
  id: string;
  version: number;
  tournament_id: string;
  invitation_code: string;
  status: string;
  max_players: number;
  expires_at: string;
  game_id?: string | null;
  searching: boolean;
  hybrid_matchmaking_available: boolean;
  settings: Record<string, unknown>;
  selected_packets: LobbyPacket[];
  packet_suggestions: LobbyPacket[];
  members: LobbyMember[];
  viewer: LobbyMember;
  validation_violations: Array<{ code: string; details?: Record<string, unknown> }>;
  available_actions: string[];
  mutable_parameters: string[];
  poll_after_seconds: number;
  last_event_sequence: number;
}

export interface OngoingLobbyMember {
  display_name: string;
  role: "player" | "observer";
  ready: boolean;
}

export interface OngoingLobby {
  id: string;
  version: number;
  tournament_id: string;
  tournament_name: string;
  invitation_code: string;
  max_players: number;
  searching: boolean;
  expires_at: string;
  members: OngoingLobbyMember[];
  selected_packets: Array<{
    packet_id: string;
    name: string;
    lead_author: string | null;
    year: number | null;
    fresh_play_unit_count: number;
    total_play_unit_count: number;
    playable_for_all: boolean;
  }>;
  is_member: boolean;
  viewer_role: "player" | "observer" | null;
  viewer_manages: boolean;
}

export interface OngoingGame {
  id: string;
  tournament_id: string;
  tournament_name: string;
  status: string;
  phase: string;
  participant_count: number;
  participants: string[];
  observing: boolean;
  observing_policy: string;
  managed: boolean;
  fresh_content_count: number;
  confirmation_required: boolean;
  can_observe: boolean;
}

export interface GameObservation {
  game_id: string;
  joined: boolean;
  confirmation_required: boolean;
  fresh_content_count: number;
}

export interface OngoingResource {
  kind: "ongoing";
  state: "ready" | "empty";
  lobbies: OngoingLobby[];
  games: OngoingGame[];
}

export interface TournamentRegistrationPayload {
  accepted: boolean;
  status: string;
  reasons: string[];
}

export interface PacketQuestion {
  value: number;
  form: string;
  text: string;
  answer: string;
  accepted_answers: string[];
  rejected_answers?: string[];
  commentary: string;
  source: string;
  author: string;
  id?: string | null;
  author_id?: string | null;
}

export interface PacketTheme {
  name: string;
  author: string;
  commentary?: string;
  questions: PacketQuestion[];
}

export interface PacketContent {
  name: string;
  language: string;
  lead_author: string;
  year: number | null;
  themes: PacketTheme[];
}

export interface PacketEditorDescriptor {
  schema: string;
  page_collection: "themes";
  packet_fields: string[];
  theme_fields: string[];
  question_fields: string[];
  question_values: number[];
}

export interface PacketDraftResource {
  kind: "packet_draft";
  state: "ready";
  draft_id: string;
  version: number;
  status: string;
  source_filename: string;
  ruleset_key: string;
  ruleset_version: number;
  errors: string[];
  warnings: string[];
  can_publish: boolean;
  can_reject: boolean;
  packet: PacketContent;
  editor: PacketEditorDescriptor;
  author_bindings: Record<string, string>;
  lead_author_id: string | null;
  associated_authors: RegisteredAuthor[];
  assignment_id?: string;
  field_author_ids?: Record<string, string | null>;
}

export interface RegisteredAuthor {
  author_id: string;
  display_name: string;
}

export interface AuthorshipMetadata {
  packet_count: number;
  theme_count: number;
  question_count: number;
  packet_names: string[];
  theme_names: string[];
  tournament_names: string[];
  years: number[];
}

export interface AuthorSummary {
  author_id: string;
  display_name: string;
  authorship: AuthorshipMetadata;
}

export interface AuthorSearchPage {
  items: AuthorSummary[];
  next_cursor: string | null;
}

export interface AuthorLinkRequest {
  request_id: string;
  player_id: string;
  player_nickname: string;
  author: AuthorSummary;
  status: string;
  request_note: string | null;
  decision_note: string | null;
  decided_by_id: string | null;
  created_at: string;
  decided_at: string | null;
  cancelled_at: string | null;
}

export interface AuthorLinksResource {
  kind: "author_links";
  state: "ready" | "empty";
  items: AuthorLinkRequest[];
  next_cursor?: string | null;
}

export interface PublicAuthor {
  id: string;
  display_name: string;
  tournament_count: number;
  question_count: number;
}

export interface AuthorsResource {
  kind: "authors";
  state: "ready";
  items: PublicAuthor[];
}

export interface AuthorStatistics {
  presentations: number;
  solved: number;
  exposures: number;
  buzzes: number;
  attempts: number;
  correct: number;
  timeouts: number;
  buzz_rate: number | null;
  accuracy: number | null;
  solved_rate: number | null;
}

export interface AuthorProfileResource {
  kind: "author_profile";
  state: "ready";
  author: PublicAuthor;
  statistics: AuthorStatistics;
  by_value: Array<AuthorStatistics & { value: number }>;
}

export interface PageCursor {
  previous?: string;
  next?: string;
}

export interface LibraryPacket {
  packet_id: string;
  version_id: string;
  name: string;
  year: number | null;
  published_at: string;
  lead_author: string;
  authors: string[];
  fresh_play_unit_count: number;
  total_play_unit_count: number;
  tournaments: Array<{ id: string; name: string; slug: string; role: "player" | "manager" }>;
}

export interface LibraryResource {
  kind: "library";
  state: "ready";
  items: LibraryPacket[];
}

export interface LibraryPage {
  title: string;
  author: string;
  commentary?: string;
  author_id?: string | null;
  questions: PacketQuestion[];
}

export interface LibraryQuestionStatistics {
  views: number;
  buzzes: number;
  attempts: number;
  correct: number;
  incorrect: number;
  correct_rate: number | null;
  incorrect_rate: number | null;
}

export type LibraryAccess =
  | { confirmation_required: true; fresh_unit_count: number }
  | { confirmation_required: false; name: string; pages: LibraryPage[]; statistics?: Record<string, LibraryQuestionStatistics> }
  | { confirmation_required: false; queued: true };

export interface PlayerProfileIdentity {
  id: string;
  nickname: string | null;
  real_name?: string | null;
  telegram_username?: string | null;
  telegram_public?: boolean;
  viewer_privileged: boolean;
}

export interface PlayerRuleset {
  key: string;
  name: string;
}

export interface PlayerRating {
  value: number;
  history: Array<{ played_at: string; rating: number }>;
}

export type PlayerPlacementKind =
  | "place_1"
  | "place_2"
  | "place_3"
  | "place_4"
  | "place_1_5"
  | "place_2_5"
  | "place_3_5"
  | "below_4";

export interface PlayerPlacement {
  kind: PlayerPlacementKind;
  count: number;
  percent: number;
}

export interface PlayerProfileGameParticipant {
  participant_id: string;
  player_id: string;
  nickname: string | null;
  score: number;
  place: number | null;
  global_rating_after?: number | null;
  tournament_rating_after?: number | null;
}

export interface PlayerProfileGamePacket {
  name: string | null;
}

export interface PlayerProfileGame {
  game_id: string;
  tournament_id: string;
  tournament_name: string | null;
  stage: string | null;
  played_at: string | null;
  participants: PlayerProfileGameParticipant[];
  packets?: PlayerProfileGamePacket[] | null;
}

export interface PlayerQuestionStat {
  value: number;
  correct: number;
  incorrect: number;
}

export interface PlayerSiBuzzTime {
  value: number;
  average_seconds: number | null;
  samples: number;
}

export interface PlayerSiStatistics {
  average_normalized_score: number | null;
  buzz_times: PlayerSiBuzzTime[];
}

export interface PlayerProfileResource {
  kind: "player_profile";
  state: "ready" | "empty";
  player: PlayerProfileIdentity;
  rulesets: PlayerRuleset[];
  ruleset_key: string | null;
  rating: PlayerRating;
  stats: {
    games: number;
    wins: number;
    win_rate: number;
    placements: PlayerPlacement[];
  };
  si_question_stats: PlayerQuestionStat[] | null;
  si_statistics?: PlayerSiStatistics | null;
  games: PlayerProfileGame[];
}

export interface PlayerGameTheme {
  index: number;
  questions: Array<{
    value: number;
    answers: Record<string, "correct" | "incorrect">;
  }>;
}

export interface PlayerGameResource {
  kind: "player_game";
  state: "ready" | "empty";
  game_id: string;
  player_id: string;
  tournament_name: string | null;
  tournament_visible: boolean;
  stage: string | null;
  played_at: string | null;
  participants: PlayerProfileGameParticipant[];
  packets?: PlayerProfileGamePacket[] | null;
  themes: PlayerGameTheme[];
}

export interface SuspicionRulesetStat {
  ruleset_key: string;
  rating: number | null;
  games_played: number;
}

export interface SuspicionLedgerCard {
  player_id: string;
  display_name: string | null;
  telegram_username: string | null;
  suspicion: number;
  rulesets: SuspicionRulesetStat[];
  reports: Array<{ kind: string; count: number }>;
}

export interface SuspicionEvidence {
  signal: string;
  ruleset_key: string;
  summary: Record<string, unknown>;
  created_at: string;
}

export interface SuspicionEvent {
  id: number | string;
  reason: string;
  ruleset_key: string | null;
  delta: number;
  before: number;
  after: number;
  note: string | null;
  created_at: string;
  evidence: SuspicionEvidence[];
}

export interface AdminSuspicionLedgerResource {
  kind: "admin_suspicion_ledger";
  state: "ready" | "empty";
  items: SuspicionLedgerCard[];
}

export type AdminSection =
  | "tournaments"
  | "authors"
  | "players"
  | "packets"
  | "link_requests"
  | "ongoing_games";
export type AdminValue = string | number | boolean | null | AdminValue[] | { [key: string]: AdminValue };
export interface AdminCard {
  id: string;
  [key: string]: AdminValue;
}
export interface AdminManagementResource {
  kind: "admin_management";
  state: "ready";
  section: AdminSection;
  items: AdminCard[];
}

export interface AdminSuspicionInspectionPayload {
  player: {
    id: string;
    display_name: string | null;
    telegram_username: string | null;
    suspicion: number;
  };
  events: SuspicionEvent[];
}

export interface RoutePayload {
  locale: Locale;
  authorization: RouteAuthorization;
  resource:
    | RouteResource
    | TournamentRouteResource
    | TournamentProfileResource
    | TournamentPacketsResource
    | TournamentChatResource
    | TournamentManagerSettingsResource
    | TournamentManagerManagementResource
    | LobbyResource
    | OngoingResource
    | PacketDraftResource
    | LibraryResource
    | PlayerProfileResource
    | PlayerGameResource
    | AdminSuspicionLedgerResource
    | AdminManagementResource
    | AuthorLinksResource
    | AuthorsResource
    | AuthorProfileResource;
  pagination?: PageCursor;
}

export interface TournamentChatResource {
  kind: "tournament_chat";
  state: "ready";
  chat_id: string;
  tournament_id: string;
  tournament_name: string;
  round_number: number;
  match_number: number;
  multiple_matches: boolean;
  planned_at?: string | null;
  planned_by_id?: string | null;
  participants: Array<{ player_id: string; nickname: string }>;
  unread?: boolean;
}

export interface TournamentPacket {
  packet_id: string;
  version_id: string;
  name: string;
  year: number | null;
  published_at: string | null;
  lead_author: string | null;
  authors: string[];
  playable: boolean;
  blocked: boolean;
  library_viewable: boolean;
  total_play_unit_count: number;
  fresh_play_unit_count: number;
}

export interface TournamentPacketsResource {
  kind: "tournament_packets";
  state: "ready" | "empty";
  tournament_id: string;
  tournament_name: string;
  items: TournamentPacket[];
}

export interface RequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
  idempotencyKey?: string;
  retryAuthentication?: boolean;
  signal?: AbortSignal;
}
