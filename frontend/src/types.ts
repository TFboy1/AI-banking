export interface Account {
  id: string;
  name: string;
  kind: string;
  balance_minor: number;
}
export interface Card {
  id: string;
  account_id: string;
  name: string;
  last4: string;
  status: string;
  daily_limit_minor: number;
}
export interface Holding {
  product_id: string;
  name: string;
  units: string;
  nav: string;
  value_minor: number;
  risk_level: number;
}
export interface Transaction {
  id: string;
  counterparty: string;
  direction: string;
  amount_minor: number;
  category: string;
  occurred_at: string;
}
export interface Subscription {
  id: string;
  name: string;
  amount_minor: number;
  status: string;
  next_charge_date: string;
}
export interface BankSnapshot {
  accounts: Account[];
  cards: Card[];
  holdings: Holding[];
  transactions: Transaction[];
  subscriptions: Subscription[];
  total_assets_minor: number;
  risk_level: number | null;
  current_month: string;
  protected_minor?: number;
  free_minor?: number;
  business_date?: string;
  next_salary_period?: string;
}
export interface Field {
  label: string;
  value: string;
}
export interface Receipt {
  action_id: string;
  title: string;
  executed_at: string;
  fields: Field[];
}
export interface Display {
  title: string;
  columns: string[];
  rows: string[][];
  metrics: Field[];
  note: string;
}
export interface DecisionCheck {
  key?: string;
  label: string;
  status: "pass" | "block" | "unknown";
  detail: string;
  references?: string[];
}
export interface Explanation {
  as_of: string;
  sources: { label: string; references: string[] }[];
  checks: DecisionCheck[];
  conclusion: string;
}
export interface Diagnosis {
  title: string;
  status: "ready" | "blocked" | "unknown";
  checks: DecisionCheck[];
  next_steps: { label: string; prompt: string }[];
}
export interface ToolResult {
  display?: Display;
  alerts?: { text: string; references: string[] }[];
  holdings?: Holding[];
  diagnosis?: Diagnosis;
  explanation?: Explanation;
}
export interface MessageData {
  channel?: string;
  source?: string;
  tool?: string;
  result?: ToolResult;
  approval_id?: string;
  status?: string;
  expires_at?: string;
  title?: string;
  fields?: Field[];
  amount_minor?: number;
  receipt?: Receipt;
  explanation?: Explanation;
  error?: string;
  card_type?: "preview" | "challenge" | "trigger";
  plan?: Plan;
  plan_id?: string;
  version?: number;
  challenge?: Challenge;
  notification_id?: string;
  event_id?: string;
}
export interface Message {
  id: string;
  conversation_id: string;
  task_id: string | null;
  role: "user" | "assistant";
  kind: "text" | "data" | "approval" | "error" | "receipt" | "plan" | "diagnosis";
  text: string;
  data: MessageData;
  created_at: string;
}
export interface Task {
  id: string;
  conversation_id?: string;
  status: string;
  error?: string;
}
export interface Conversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  preview: string | null;
  task_status: string | null;
  unread_count?: number;
}
export interface AppConfig {
  model_configured: boolean;
  model_name: string;
  feishu_enabled: boolean;
  feishu_bound: boolean;
}

export interface CashflowResult {
  feasible: boolean;
  opening_available_minor: number;
  ending_balance_minor: number;
  ending_protected_minor: number;
  ending_available_minor: number;
  lowest_available_minor: number;
  lowest_date: string;
  first_failure: { date: string; event: string; shortfall_minor: number } | null;
  timeline: {
    key: string;
    kind: "income" | "expense" | "action";
    date: string;
    label: string;
    amount_minor: number;
    balance_minor: number;
    protected_minor: number;
    available_minor: number;
  }[];
}

export interface PlanVariant {
  key: "now" | "after_salary";
  label: string;
  action_date: string;
  result: CashflowResult;
}

export interface Plan {
  plan_id: string;
  version: number;
  title: string;
  kind: "purchase" | "savings";
  status: string;
  amount_minor: number;
  living_reserve_minor: number;
  deadline: string;
  assumptions: {
    as_of: string;
    salary?: { amount_minor: number; day: number; evidence_ids: string[]; source?: string } | null;
    rent?: { amount_minor: number; day: number; evidence_ids: string[]; source?: string } | null;
    subscriptions: Subscription[];
  };
  variants: PlanVariant[];
  installments?: { date: string; amount_minor: number }[];
  executed_minor?: number;
  explanation?: Explanation;
}

export interface Challenge {
  dimension: "salary_delay" | "unexpected_expense";
  found: boolean;
  scope?: string;
  minimum_days?: number;
  previous_days_passed?: number | null;
  maximum_minor?: number;
  first_failure_minor?: number;
  date?: string;
  failure?: { date: string; event: string; shortfall_minor: number };
  reason?: string;
  repair?: { date: string; delay_days: number; result: CashflowResult } | null;
}
export interface Bootstrap {
  conversation_id: string;
  conversations: Conversation[];
  messages: Message[];
  event_id: number;
  active_task: Task | null;
  bank: BankSnapshot;
  config: AppConfig;
}
export type InspectTool =
  | "get_accounts"
  | "analyze_bills"
  | "list_products"
  | "list_cards"
  | "list_subscriptions";

export const money = (minor: number) =>
  (minor / 100).toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
export const timeLabel = (date: string) =>
  new Date(date).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Asia/Shanghai",
  });

export async function api<T>(
  path: string,
  body?: unknown,
  method?: "POST" | "PATCH" | "DELETE",
): Promise<T> {
  let response: Response;
  try {
    response = await fetch("/api" + path, {
      method: method || (body === undefined ? "GET" : "POST"),
      credentials: "same-origin",
      headers:
        body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new Error("无法连接本地银行服务，请确认后端已启动。");
  }
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error("银行服务返回了无效响应，请确认服务已正常启动。");
  }
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : `请求未完成（${response.status}）`,
    );
  return data as T;
}
