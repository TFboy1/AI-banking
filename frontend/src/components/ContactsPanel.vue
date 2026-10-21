<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { Eye, EyeOff, Pencil, Users, X, Plus } from "@lucide/vue";
import { api } from "../types";
import type { Contact } from "../types";
const props = defineProps<{ busy: boolean; revision?: number }>();
const emit = defineEmits<{ close: []; select: [id: string] }>();
const contacts = ref<Contact[]>([]);
const loading = ref(true);
const error = ref("");
const saving = ref(false);
const revealing = ref<string | null>(null);
const modal = ref<"create" | "supplement" | "profile" | null>(null);
const target = ref<Contact | null>(null);
const blankForm = () => ({ name: "", phone: "", bank: "", card_number: "", relationship: "", birthday: "", gender: "", special_attention: false, note: "" });
const form = ref(blankForm());
const relationships = ["家人", "朋友", "爱人", "同事", "熟人", "其他", "未分类"];
const category = ref("全部");
const filteredContacts = computed(() => contacts.value.filter(person => category.value === "全部" || (category.value === "特别关注" ? person.special_attention : person.relationship === category.value)));
function openProfile(person: Contact) { modal.value = "profile"; target.value = person; form.value = {...blankForm(), relationship: person.relationship === "未分类" ? "" : person.relationship, birthday: person.birthday || "", gender: person.gender === "未设置" ? "" : person.gender, special_attention: person.special_attention, note: person.note }; duplicates.value = []; modalError.value = ""; }
const duplicates = ref<Contact[]>([]);
const modalError = ref("");
function openCreate() { modal.value = "create"; target.value = null; form.value = blankForm(); duplicates.value = []; modalError.value = ""; }
function openSupplement(person: Contact) { modal.value = "supplement"; target.value = person; form.value = blankForm(); duplicates.value = []; modalError.value = ""; }
function replaceContact(person: Contact) {
  const index = contacts.value.findIndex(c => c.id === person.id);
  if (index < 0) contacts.value.push(person); else contacts.value[index] = person;
  masked.set(person.id, { ...person });
}
async function addContact(choice?: string) {
  if (saving.value) return;
  saving.value = true; modalError.value = "";
  try {
    if (modal.value === "create") {
      if (!choice) {
        const matches = await api<Contact[]>("/contacts/check-name", { name: form.value.name.trim() });
        if (matches.length) { duplicates.value = matches; return; }
      }
      const body = { name: form.value.name.trim(), phone: form.value.phone || null, bank: form.value.bank.trim() || null, card_number: form.value.card_number.replace(/\s/g, "") || null, relationship: form.value.relationship || null, birthday: form.value.birthday || null, gender: form.value.gender || null, special_attention: form.value.special_attention ? true : null, note: form.value.note || null, ...(choice === "new" ? { new_person: true } : choice ? { same_person_id: choice } : {}) };
      replaceContact(await api<Contact>("/contacts", body));
    } else if (modal.value === "profile" && target.value) {
      replaceContact(await api<Contact>(`/contacts/${encodeURIComponent(target.value.id)}/profile`, {relationship:form.value.relationship || "未分类", birthday:form.value.birthday || null, gender:form.value.gender || "未设置", special_attention:form.value.special_attention, note:form.value.note}, "PATCH"));
    } else if (target.value) {
      replaceContact(await api<Contact>(`/contacts/${encodeURIComponent(target.value.id)}/details`, {
        phone: form.value.phone || null, bank: form.value.bank.trim() || null, card_number: form.value.card_number.replace(/\s/g, "") || null,
      }));
    }
    modal.value = null;
  } catch (e) { modalError.value = (e as Error).message; }
  finally { saving.value = false; }
}
const masked = new Map<string, Contact>();
async function loadContacts() {
  try { contacts.value = await api<Contact[]>("/contacts"); contacts.value.forEach(c => masked.set(c.id, { ...c })); }
  catch (e) { error.value = (e as Error).message; }
  finally { loading.value = false; }
}
onMounted(loadContacts);
watch(() => props.revision, loadContacts);
async function toggleReveal(person: Contact) {
  if (revealing.value) return;
  if (person.revealed) { Object.assign(person, masked.get(person.id)); return; }
  revealing.value = person.id;
  error.value = "";
  try { Object.assign(person, await api<Contact>(`/contacts/${encodeURIComponent(person.id)}`)); }
  catch (e) { error.value = (e as Error).message; }
  finally { revealing.value = null; }
}
</script>
<template>
  <aside class="bank-panel contacts-panel" aria-label="联系人面板">
    <div class="panel-heading"><button class="icon-button" aria-label="新增联系人" @click="openCreate"><Plus :size="20" /></button><div><span class="small-eyebrow">随时找到要联系的人</span><h2>联系人 <span class="contacts-count">{{ contacts.length }} 人</span></h2></div><button class="icon-button" aria-label="收起联系人" @click="emit('close')"><X :size="19" /></button></div>
    <div class="panel-content contacts-content">
      <p class="contacts-hint">默认隐藏个人信息，点击眼睛展开。点击联系人开始对话。</p>
      <div class="contact-categories" aria-label="联系人分类"><button v-for="tag in ['全部', '特别关注', ...relationships]" :key="tag" :aria-pressed="category === tag" :class="{ selected: category === tag }" @click="category = tag">{{ tag }}<span>{{ contacts.filter(p => tag === '全部' || (tag === '特别关注' ? p.special_attention : p.relationship === tag)).length }}</span></button></div>
      <p v-if="loading" role="status">正在加载联系人…</p>
      <p v-if="error" class="contacts-error" role="alert">{{ error }}</p>
      <article v-for="person in filteredContacts" :key="person.id" class="contact-card">
        <div class="contact-main-row">
          <button class="contact-select" :disabled="props.busy || saving" @click="emit('select', person.id)">
            <span class="contact-avatar"><Users :size="17" /></span>
            <span class="contact-identity"><strong>{{ person.note || person.name }}</strong><span v-if="person.note">原名：{{ person.name }}</span><span v-for="phone in person.phones" :key="phone">{{ phone }}</span></span>
          </button>
          <button class="icon-button" aria-label="补充电话或银行卡" @click="openSupplement(person)"><Plus :size="17" /></button>
          <button class="icon-button contact-eye" :disabled="!!revealing" :aria-label="person.revealed ? '隐藏联系人完整信息' : '查看联系人完整信息'" :aria-pressed="person.revealed" @click="toggleReveal(person)"><EyeOff v-if="person.revealed" :size="17" /><Eye v-else :size="17" /></button>
        </div>
        <div class="contact-profile-tags"><span>{{ person.relationship }}</span><span v-if="person.special_attention">★ 特别关注</span><span v-if="person.gender !== '未设置'">{{ person.gender }}</span><span v-if="person.birthday">生日 {{ person.birthday }}</span></div>
        <button class="contact-edit" @click="openProfile(person)"><Pencil :size="13" />修改资料</button>
        <button v-if="person.accounts.length" class="contact-accounts" :disabled="props.busy || saving" @click="emit('select', person.id)"><span v-for="account in person.accounts" :key="account.contact_id" class="contact-account"><span>{{ account.bank }}</span><span>{{ account.card_number }}</span></span></button>
      </article>
      <p v-if="!loading && !filteredContacts.length && !error">{{ contacts.length ? '此类别暂无联系人。' : '暂无联系人。' }}</p>
      <p class="contacts-footnote">银行卡为演示号码。收起面板后，完整信息会重新隐藏。</p>
    </div>
  </aside>
  <Teleport to="body">
    <div v-if="modal" class="modal-backdrop contact-modal-backdrop" @click.self="!saving && (modal = null)">
      <section class="settings-dialog contact-dialog" role="dialog" aria-modal="true" aria-labelledby="contact-dialog-title">
        <div class="dialog-heading"><h2 id="contact-dialog-title">{{ modal === 'create' ? '新增联系人' : modal === 'profile' ? '修改联系人资料' : '补充电话或银行卡' }}</h2><button class="icon-button" :disabled="saving" aria-label="关闭联系人表单" @click="modal = null"><X :size="18" /></button></div>
        <form class="contact-create-form" @submit.prevent="addContact()">
          <template v-if="!duplicates.length"><label v-if="modal === 'profile'">联系人备注（原名会保留）<input v-model="form.note" maxlength="120" placeholder="例如：同事小陈" /></label><label v-if="modal === 'create'">姓名<input v-model="form.name" required maxlength="60" placeholder="联系人原名" /></label><template v-if="modal !== 'supplement'"><label>关系分类<select v-model="form.relationship"><option value="">未分类</option><option v-for="relationship in relationships.filter(value => value !== '未分类')" :key="relationship" :value="relationship">{{ relationship }}</option></select></label><label>生日<input v-model="form.birthday" type="date" /></label><label>性别<select v-model="form.gender"><option value="">未设置</option><option v-for="gender in ['男','女','其他']" :key="gender" :value="gender">{{ gender }}</option></select></label><label class="contact-attention"><input v-model="form.special_attention" type="checkbox" />特别关注</label></template><template v-if="modal !== 'profile'">

            <p v-if="modal === 'supplement'" class="contacts-hint">为 {{ target?.note || target?.name }} 添加信息，已有电话和银行卡会保留。</p>
            <label>手机号<input v-model="form.phone" inputmode="tel" pattern="1[3-9][0-9]{9}" maxlength="11" placeholder="11 位手机号" /></label>
            <label>银行名称<input v-model="form.bank" :required="!!form.card_number" maxlength="60" placeholder="例如：演示银行" /></label>
            <label>银行卡号<input v-model="form.card_number" :required="!!form.bank" inputmode="numeric" pattern="[0-9 ]{12,24}" maxlength="24" placeholder="12–19 位完整卡号" /></label>
<label v-if="modal === 'create'">备注<input v-model="form.note" maxlength="120" /></label>
            <p class="contacts-hint">{{ modal === 'create' ? '姓名必填，其他信息可稍后补充。同名时会先确认是否为同一个人。' : '可只加电话；添加银行卡时需同时填写银行名称。' }}</p>
          </template></template>
          <div v-else class="contact-duplicates">
            <p>已有同名联系人“{{ form.name }}”，这是同一个人吗？</p>
            <button v-for="match in duplicates" :key="match.id" type="button" :disabled="saving" @click="addContact(match.id)"><strong>是，合并到这位联系人</strong><span>{{ match.note || match.name }} · {{ match.phone }}</span><span>{{ match.accounts.map(a => a.bank + ' ' + a.card_number.slice(-4)).join(' / ') }}</span></button>
            <button type="button" :disabled="saving" @click="addContact('new')">不是同一个人，另建联系人</button>
            <button type="button" :disabled="saving" @click="duplicates = []">返回修改信息</button>
          </div>
          <p v-if="modalError" class="contacts-error" role="alert">{{ modalError }}</p>
          <div v-if="!duplicates.length" class="contact-form-actions"><button type="button" :disabled="saving" @click="modal = null">取消</button><button type="submit" class="contact-save" :disabled="saving">{{ saving ? '保存中…' : '保存联系人' }}</button></div>
        </form>
      </section>
    </div>
  </Teleport>
</template>
