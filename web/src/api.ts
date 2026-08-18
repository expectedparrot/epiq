export type CellState = "Answered" | "Contested" | "NotFound" | "Unasked";

export interface Lineage {
  token: string;
  claim_id: string;
  value: unknown;
  confidence: "low" | "medium" | "high";
  evidence_id: string;
  source: {
    title: string;
    url: string;
    published_at?: string | null;
    retrieved_at?: string;
  };
  excerpt: string;
  as_of?: string;
  temporal_basis?: "observed" | "source" | "unknown";
  derivation?: {
    operation: string;
    parameters: Record<string, unknown>;
    input_claim_ids: string[];
    dependencies: Array<{
      claim_id: string;
      role: "operand" | "parameter" | "path";
    }>;
  };
}

export interface Cell {
  state: CellState;
  value?: unknown;
  values: unknown[];
  lineage: Lineage[];
  research?: { task_id: string; query: string; notes: string };
  references?: Array<{ entity_id: string; kind: string; name: string }>;
  temporal?: {
    volatility: "stable" | "slow" | "dynamic";
    freshness_days?: number | null;
    as_of?: string | null;
    age_days?: number | null;
    freshness: "not_applicable" | "fresh" | "stale" | "unknown";
    basis?: "observed" | "source" | "unknown";
  };
}

export interface Question {
  question_id: string;
  name: string;
  value_type: string;
  definition: Record<string, unknown>;
  schema_state?: "active" | "challenged";
  open_challenges?: Array<{
    challenge_id: string;
    problem: string;
    explanation: string;
  }>;
}

export interface Matrix {
  entity_kind: string;
  questions: Question[];
  rows: Array<{
    entity_id: string;
    name: string;
    attributes: Record<string, unknown>;
    cells: Record<string, Cell>;
  }>;
}

export interface Overview {
  project: Record<string, string>;
  entity_kinds: Array<{ kind: string; entities: number; questions: number }>;
}

export interface ProjectInfo {
  project_id: string;
  name: string;
  path: string;
  active: boolean;
}

export interface QuestionChallenge {
  challenge_id: string;
  question_id: string;
  question_name: string;
  problem: string;
  explanation: string;
  example_entity_id?: string | null;
  example_entity_name?: string | null;
  evidence_ids: string[];
  proposed_replacement?: Record<string, unknown> | null;
  status: "open" | "resolved" | "dismissed";
  resolution?: string | null;
}

export interface ResearchJob {
  job_id: string;
  entity_kind: string;
  question_id: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  total: number;
  completed: number;
  target_entity_ids: string[];
  requested_entity_ids?: string[] | null;
  instructions?: string;
  scope?: "cell" | "row" | "column" | "table";
  job_type?: "research" | "entity_suggestions" | "field_suggestions";
  mode:
    | "fill_missing"
    | "add_evidence"
    | "retry_not_found"
    | "suggest_entities"
    | "suggest_fields";
  messages: Array<{ at: string; message: string }>;
  error?: string | null;
  outcome?: "changed" | "no_change" | "proposals" | "schema_proposal" | null;
  written?: number;
  no_result?: number;
  rejected?: number;
  suggestions?: EntitySuggestion[];
  field_suggestions?: FieldSuggestion[];
  relationship_suggestions?: RelationshipSuggestion[];
  schema_adaptation?: SchemaAdaptation | null;
  deduplicated?: boolean;
}

export interface SchemaAdaptation {
  kind: "cardinality_mismatch";
  question_id: string;
  question_name: string;
  label: string;
  current_cardinality: "one";
  proposed_cardinality: "many";
  status: "pending" | "applying" | "applied" | "rejected";
  successor_question_id?: string;
}

export interface RelationshipSuggestion {
  suggestion_id: string;
  subject_entity_id: string;
  subject_name: string;
  question_id: string;
  question_name: string;
  target_kind: string;
  target_name: string;
  target_entity_id?: string | null;
  action: "link" | "create_and_link";
  source_title: string;
  source_url?: string | null;
  excerpt: string;
  confidence: "low" | "medium" | "high";
  status: "pending" | "accepted" | "dismissed";
}

export interface RelatedEntity {
  entity_id: string;
  name: string;
  kind: string;
}

export interface RelationshipEdge {
  direction: "incoming" | "outgoing";
  depth: number;
  question: string;
  claim_ids: string[];
  from: RelatedEntity;
  to: RelatedEntity;
}

export interface RelationshipGraph {
  entity: RelatedEntity;
  direction: "incoming" | "outgoing" | "both";
  depth: number;
  edges: RelationshipEdge[];
}

export interface FieldSuggestion {
  suggestion_id: string;
  name: string;
  label: string;
  value_type: string;
  rationale: string;
  research_guidance: string;
  status: "pending" | "accepted" | "dismissed";
  question_id?: string | null;
}

export interface EntitySuggestion {
  suggestion_id: string;
  name: string;
  rationale: string;
  source_title: string;
  source_url: string;
  status: "pending" | "accepted" | "dismissed";
  entity_id?: string | null;
}

export interface StaleDerivation {
  claim_id: string;
  subject: string;
  kind: string;
  question: string;
  reasons: Array<{
    dependency_claim_id: string;
    role: "operand" | "parameter" | "path";
    reason:
      "dependency_stale" | "dependency_inactive" | "newer_claim_available";
  }>;
}

export interface DiagnosticCell {
  entity_id: string;
  entity_name: string;
  question_id: string;
  question: string;
  state: CellState;
  values: unknown[];
  lineage: Lineage[];
  temporal?: Cell["temporal"];
}

export interface DiagnosticResult {
  entity_kind: string;
  count: number;
  cells: DiagnosticCell[];
}

export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public suggestion?: string,
  ) {
    super(message);
  }
}

export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options?.headers },
  });
  const body = await response.json();
  if (!response.ok) {
    const validation = Array.isArray(body.detail)
      ? body.detail
          .map(
            (item: { loc?: unknown[]; msg?: string }) =>
              `${item.loc?.slice(1).join(".") || "input"}: ${item.msg || "is invalid"}`,
          )
          .join("; ")
      : null;
    const error = body.error ?? {
      code: "invalid_request",
      message:
        validation || body.detail || `Request failed (${response.status})`,
    };
    throw new ApiError(error.code, error.message, error.suggestion);
  }
  return body as T;
}

export const post = <T>(path: string, body: unknown) =>
  api<T>(path, { method: "POST", body: JSON.stringify(body) });
