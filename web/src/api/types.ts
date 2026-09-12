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
  status: string;
  phase: "future" | "ongoing" | "past";
  visibility: "public" | "private";
  starts_at?: string | null;
  planned_ends_at?: string | null;
  actual_ends_at?: string | null;
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

export interface TournamentManagerSettingsResource {
  kind: "manager_settings";
  state: "ready";
  tournament: TournamentListItem;
  settings_version: number;
  finalized_at?: string | null;
  registration_enabled: boolean;
  ignore_late_registrations: boolean;
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

export interface TournamentManagerManagementResource {
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
  commentary: string;
  source: string;
  author: string;
}

export interface PacketTheme {
  name: string;
  author: string;
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
  questions: PacketQuestion[];
}

export type LibraryAccess =
  | { confirmation_required: true; fresh_unit_count: number }
  | { confirmation_required: false; name: string; pages: LibraryPage[] }
  | { confirmation_required: false; queued: true };

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
  resource: RouteResource | TournamentRouteResource | TournamentManagerSettingsResource | TournamentManagerManagementResource | LobbyResource | PacketDraftResource | LibraryResource | AdminSuspicionLedgerResource;
  pagination?: PageCursor;
}

export interface RequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
  idempotencyKey?: string;
  retryAuthentication?: boolean;
  signal?: AbortSignal;
}
