export interface Contact {
  id: string; name: string; phone: string; phones: string[]; card_number: string;
  accounts: { contact_id: string; bank: string; card_number: string }[];
  bank: string; note: string; revealed: boolean; simulated: boolean;
  relationship: string; birthday: string | null; gender: string; special_attention: boolean;
}
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
  balance_minor: number;
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
  expires_on?: string | null;
  expiry_estimated?: boolean;
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
  securities_cost_minor?: number;
  securities_value_minor?: number;
  securities_market_value_minor?: number | null;
  securities_profit_minor?: number | null;
  securities_valuation_complete?: boolean;
  securities_valuation_stale?: boolean;
  securities_positions?: { symbol: string; name: string; shares: number; cost_minor: number; value_minor?: number; market_value_minor?: number | null; profit_minor?: number | null; profit_percent?: number | null; price_minor?: number | null; quote_date?: string | null; source?: string | null; price_kind?: string | null; valuation_stale?: boolean }[];
  transactions: Transaction[];
  subscriptions: Subscription[];
  total_assets_minor: number;
  account_balance_minor?: number;
  category_balance_minor?: number;
  card_balance_minor?: number;
  investment_assets_minor?: number;
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
export interface BirthdayNotice {plan_id:string;conversation_id:string;title:string}
export interface BirthdayPlan {
 id:string;contact_id:string;contact_name:string;event_date:string;remind_at:string;account_id:string;account_name:string;
 budget_minor:number;reserved_minor:number;spent_minor:number;status:string;status_label:string;version:number;order_id:string|null;notify_enabled:boolean;
 steps:{id:string;title:string;depends_on:string[];status:string}[];
 recommendation:null|{message:string;menus:{shop_id:string;name:string;delivery_minor:number;place:{name:string};menu:{id:string;name:string;price_minor:number;packing_minor:number;available:boolean}[]}[]};
}
export interface Receipt {
  tool?: string;
  birthday_plan?: BirthdayPlan;
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
  partial_completion?:PartialCompletion;
  daily_digest?: {greeting:string;subtitle:string;sections:Display[]};
  analysis?: CustomBillAnalysis;
  subscriptions?: Subscription[];
  birthday_plans?: BirthdayPlan[];
  bill_report?: BillReport;
  recipients?: { id: string; name: string; detail: string }[];
  needs_clarification?: boolean;
  display?: Display;
  alerts?: { text: string; references: string[]; transactions?: BillTransaction[]; baseline_transactions?: BillTransaction[] }[];
  holdings?: Holding[];
  diagnosis?: Diagnosis;
  explanation?: Explanation;
}
export interface BillTransaction {
  id: string;
  occurred_at: string;
  counterparty: string;
  direction: string;
  amount_minor: number;
  category: string;
  description: string;
}
export interface PartialCompletion {
 task_id:string;version:number;decision:'pending'|'kept'|'replanning';reason:string;note:string;can_replan:boolean;
 debit_minor:number;credit_minor:number;
 completed:{approval_id:string;tool:string;title:string;settlement:string;debit_minor:number;credit_minor:number;order_id:string|null;order_status:string|null;can_cancel:boolean;refund_status:string|null;refunded_minor:number}[];
 unfinished:{title:string;status:string}[];
}
export interface AnalysisChartSpec {title:string;type:'line'|'donut'|'bar';labels:string[];series:{name:string;values:number[]}[]}
export interface CustomBillAnalysis {
 title:string;chart_type:'line'|'donut'|'bar';labels:string[];series:{name:string;values:number[]}[];metrics:Field[];as_of:string;
 coverage:{label:string;start:string;end:string;count:number;partial:boolean}[];difference:null|{amount_minor:number;percent:number|null};
 record_count:number;note:string;verified:boolean;rows:{label:string;values:number[]}[];
 sandbox:{run_id:string;code_hash:string;steps:number;elapsed_ms:number;mode:string};
}
export interface BillReport {
  charts?: AnalysisChartSpec[];
  trend: {month:string; income_minor:number; expense_minor:number; net_minor:number; record_count:number; status:string}[];
  top_merchants: {name:string; amount_minor:number; count:number; references:string[]}[];
  historical_coverage: string;
  record_count: number;
  anomaly_count: number;
  rule_version: string;
}
export interface MessageData {
  queried_at?: string;
  daily_report?: boolean;
  scheduled?: boolean;
  operator?: string;
  handoff_rating?: {half_stars: number | null; updated_at?: string};
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
  interruption?: { interrupted: boolean; stage: string; debit_minor: number; credit_minor: number; external_result_uncertain: boolean } | null;
  dag?: { version: number; mode: string; nodes: { id: string; title: string; tool: string; status: string; depends_on: string[]; receipt_id?: string | null; error?: string }[] } | null;
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
  human_session?: {id:string;status:string;operator?:string} | null;
  conversation_id: string;
  conversations: Conversation[];
  messages: Message[];
  event_id: number;
  active_task: Task | null;
  latest_task_plan?: Task | null;
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
  pinRetry = false,
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
  if (response.status === 401) authExpired?.();
  if (response.headers.get('X-System-Locked') === '1') systemLockedHandler?.();
  if (response.status === 428 && !pinRetry && requestTransactionPin && body && typeof body === "object") {
    const payment = await requestTransactionPin(path, body as Record<string, unknown>);
    if (!payment) throw new Error("已取消交易密码验证，操作尚未执行。");
    return api<T>(path, { ...body, ...payment }, method, true);
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
  if (data?.system_locked) systemLockedHandler?.();
  return data as T;
}

export interface PaymentCredentials { pin?: string; face_token?:string; account_id?: string; sms_code?: string; execute_at?: string }
export interface PaymentContext { face_enabled?:boolean; projected_daily_transfer_minor?:number; scheduled_preverification?:boolean; schedule_allowed?: boolean; execute_at?: string; tool?: string; basis?: string; transaction_minor?: number; level: string; sms_required: boolean; threshold_minor?: number; daily_transfer_minor?: number; title: string; amount_minor?: number; fields: Field[]; account_required: boolean; account_id: string; accounts: { id: string; name: string; balance_minor: number; eligible: boolean; reason: string }[] }
let requestTransactionPin: ((path: string, body: Record<string, unknown>) => Promise<PaymentCredentials | null>) | null = null;
export function setTransactionPinRequester(requester: (path: string, body: Record<string, unknown>) => Promise<PaymentCredentials | null>) { requestTransactionPin = requester; }

export interface AuthState {
  face_onboarding_done?:boolean;
  face_enabled?:boolean;
  system_locked?: boolean;
  registered: boolean; authenticated: boolean; has_pin: boolean; contacts_done: boolean;
  lock_enabled: boolean; ready: boolean; username: string; phone_masked: string;
}

let authExpired: (() => void) | null = null;
export function setAuthExpiredHandler(handler: () => void) { authExpired = handler; }
let systemLockedHandler: (()=>void)|null=null;
export function setSystemLockedHandler(handler:()=>void){systemLockedHandler=handler;}
