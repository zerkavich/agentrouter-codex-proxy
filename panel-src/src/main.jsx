import { t, getLanguage, setLanguage, locale } from './i18n.js';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import * as AlertDialog from '@radix-ui/react-alert-dialog';
import { Activity, ArrowDownToLine, ArrowUpRight, Check, ChevronDown, ChevronRight, CircleCheck,
  Clock3, Code2, Gauge, Info, Link2, Moon, Pause, Play, Power, Radio, RefreshCw,
  Settings2, ShieldCheck, Sun, Terminal, Timer, Waves, WifiOff, X, Zap, PackageCheck } from 'lucide-react';
import clsx from 'clsx';
import { twMerge } from 'tailwind-merge';

const cn = (...values) => twMerge(clsx(values));
const updatePending = update => ['checking', 'preparing'].includes(update?.state);
const numbers = { ru: new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }), en: new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 }) };
const count = value => Number.isFinite(value) ? numbers[getLanguage()].format(value) : '—';
const elapsed = seconds => !Number.isFinite(seconds) ? '—' : seconds >= 3600
  ? t("{v0} ч {v1} мин", {v0: (Math.floor(seconds / 3600)), v1: (Math.floor(seconds % 3600 / 60))}) : seconds >= 60
    ? t("{v0} мин {v1} с", {v0: (Math.floor(seconds / 60)), v1: (Math.floor(seconds % 60))}) : t("{v0} с", {v0: (count(seconds))});
const millis = value => !Number.isFinite(value) ? '—' : value >= 1000 ? t("{v0} с", {v0: (count(value / 1000))}) : t("{v0} мс", {v0: (count(value))});
const formatBytes = value => !Number.isFinite(value) ? '—' : value >= 1048576 ? t('{v0} МиБ', {v0: count(value / 1048576)}) : value >= 1024 ? t('{v0} КиБ', {v0: count(value / 1024)}) : t('{v0} Б', {v0: count(value)});
const dates = { ru: new Intl.DateTimeFormat('ru-RU', { dateStyle: 'short', timeStyle: 'medium' }), en: new Intl.DateTimeFormat('en-US', { dateStyle: 'short', timeStyle: 'medium' }) };
const dateTime = seconds => Number.isFinite(seconds) ? dates[getLanguage()].format(new Date(seconds * 1000)) : '—';
const explanations = {
  worker_unavailable: 'Рабочий процесс не отвечает. Используйте восстановление в независимой панели.',
  history_unavailable: 'История сейчас недоступна. Проверьте настройку записи и доступность диска.',
  history_report_unavailable: 'Этот автоматический отчёт больше не хранится.',
  history_attempts_unavailable: 'Эти детали попыток больше не хранятся.',
  invalid_history_filter: 'Проверьте параметры страницы истории.',
  recovery_requires_confirmation: 'Подтвердите восстановление рабочего процесса.',
  history_clear_requires_confirmation: 'Подтвердите очистку истории прокси.',
  panel_unavailable: 'Не удалось прочитать данные панели. Попробуйте ещё раз.',
  update_busy: 'Пакет ещё подготавливается. Дождитесь завершения проверки.',
  bootstrap_update_requires_admin: 'В обновлении изменился загрузчик восстановления. Нужна установка администратором.',
  update_probe_failed: 'Пакет не прошёл проверку запуска. Текущая версия продолжает работать.',
  update_prepared_changed: 'Подготовленный пакет изменился. Установка остановлена.',
  update_prepared_invalid: 'Подготовленный пакет не прошёл проверку.',
  update_rollback_failed: 'Автоматический откат не завершился. Нужна проверка сервиса администратором.',
  panel_session_expired: 'Прокси перезапустился. Обновляем подключение к панели.',
  settings_conflict: 'Настройки изменились в другой вкладке. Обновите значения перед сохранением.',
  invalid_setting: 'Проверьте значения отмеченных настроек.', unknown_setting: 'Эта настройка не поддерживается.',
  gzip_jobs_below_workers: 'Лимит задач должен быть не меньше количества потоков сжатия.',
  backoff_max_below_base: 'Максимальная пауза должна быть не меньше начальной.',
  restart_requires_supervisor: 'Перезапуск доступен при запуске через service.py. Сейчас прокси запущен напрямую.',
  restart_already_pending: 'Перезапуск уже ожидает завершения запросов.',
  continuous_profile_requires_settings: 'Постоянное измерение включено в настройках. Выключите его и перезапустите прокси.',
  settings_save_failed: 'Не удалось сохранить настройки. Проверьте права сервиса на папку состояния.',
  settings_unreadable: 'Не удалось прочитать сохранённые настройки.', settings_invalid: 'Файл сохранённых настроек повреждён.',
  update_check_unavailable: 'GitHub не ответил. Попробуйте проверить обновления позже.',
  restart_signal_failed: 'Не удалось передать команду перезапуска сервису.',
  local_panel_only: 'Панель доступна только на этом компьютере.', invalid_profile_duration: 'Выберите длительность от 1 до 300 секунд.',
  updates_require_prepared_install: 'Для установки из браузера нужен общий сервис с доступом к файлам обновления. Используйте подготовленный установщик.',
  update_ci_not_passed: 'Тесты новой версии ещё не прошли на GitHub. Проверим обновление позже.',
  update_changed_check_again: 'В main появился новый коммит. Проверьте обновления ещё раз.',
  runtime_drift: 'Файлы установленного прокси изменены вручную. Обновление остановлено, чтобы сохранить правки.',
  dependency_update_requires_admin: 'Эта версия меняет зависимости Python. Используйте установщик с проверкой нового окружения.',
  update_prepare_failed: 'Не удалось подготовить пакет. Попробуйте ещё раз.',
  update_staging_requires_review: 'Сохранённый пакет неполный или изменён. Нужна проверка перед повторной установкой.',
  update_package_invalid: 'Проверка пакета обновления не прошла. Установленные файлы сохранены.',
  update_not_prepared: 'Сначала подготовьте обновление, затем подтвердите установку.',
};
const findText = {
  upstream_rate_limit: 'Провайдер ограничил частоту запросов. Прокси соблюдает указанную паузу.',
  upstream_capacity_wait: 'Запросы ждут свободное соединение. Активные ответы продолжаются.',
  upstream_headers_wait: 'Провайдер ещё не прислал заголовки некоторых ответов.',
  pool_timeouts_observed: 'С момента запуска были таймауты соединений. Это не означает, что очередь заблокирована сейчас.',
  credential_partition_overflow: 'Превышен лимит независимых очередей ключей. Дополнительные запросы используют общую очередь.',
  gzip_rejection_cached: 'Провайдер недавно отклонил gzip. Для этих запросов сжатие временно пропускается.',
};
const stages = {
  client_upload: ['Получение запроса', 'Чтение тела запроса из Codex'],
  payload_inspect: ['Проверка истории', 'Проверка и ограничение исторических изображений'],
  payload_prepare: ['Подготовка и gzip', 'Подготовка байтов, включая сжатие и очередь потоков'],
  rate_limit_gate: ['Очередь после 429', 'Ожидание лимита и интервала запуска'],
  retry_backoff: ['Пауза перед повтором', 'Восстановление после временных ошибок'],
  upstream_admission: ['Свободное соединение', 'Ожидание одного из 50 слотов'],
  upstream_connect: ['TCP / DNS', 'Установка нового соединения'],
  upstream_tls: ['TLS', 'Защищённое соединение с провайдером'],
  upstream_upload: ['Отправка тела', 'Запись запроса в транспорт'],
  upstream_headers: ['Ожидание провайдера', 'Время до заголовков, включая сеть и работу провайдера'],
  upstream_body: ['Чтение JSON', 'Чтение обычного ответа или ошибки'],
  sse_prime: ['Начало стрима', 'Проверка первых событий ответа'],
  response_ready: ['До готового ответа', 'Включает этапы подготовки, ожидание и повторы'],
  sse_delivery: ['Передача стрима', 'Включает генерацию провайдера и закрытие ответа'],
};
const settingsCopy = {
  PROXY_REQUEST_GZIP: ['Сжимать большие запросы', 'Уменьшает отправляемый объём, если провайдер поддерживает gzip.'],
  PROXY_REQUEST_GZIP_MIN_BYTES: ['Порог сжатия, байт', 'Меньшие запросы отправляются сразу.'],
  PROXY_GZIP_WORKERS: ['Потоки сжатия', 'Выделенные потоки, отдельно от DNS и сети.'],
  PROXY_GZIP_MAX_JOBS: ['Лимит задач сжатия', 'При заполнении лимита запрос отправляется без сжатия.'],
  PROXY_GZIP_REJECT_TTL_S: ['Память об отказе gzip, с', 'После HTTP 415 не повторять сжатие в течение этого времени.'],
  PROXY_GZIP_UPLOAD_MBIT_S: ['Скорость отправки, Мбит/с', '0 — автоматическая оценка по реальным запросам.'],
  PROXY_GZIP_COST_MARGIN: ['Запас выгоды сжатия', 'Экономия передачи должна превышать цену сжатия в это число раз.'],
  PROXY_CONNECT_TIMEOUT_S: ['Подключение: таймаут, с', 'Ожидание подключения к провайдеру.'],
  PROXY_READ_TIMEOUT_S: ['Чтение: таймаут, с', 'Допускает длительные ответы без преждевременного обрыва.'],
  PROXY_VERBOSE: ['Подробные логи', 'Обычно выключено: меньше работы на каждый запрос.'],
  PROXY_BODY_BUDGET_BYTES: ['Бюджет больших запросов, байт', 'Оценка памяти тел и преобразований. 0 выключает очередь. Слишком большой запрос выполняется один; RAM всего процесса может быть выше бюджета.'],
  PROXY_BODY_BUDGET_WAIT_S: ['Ожидание памяти, с', 'При заполнении бюджета крупный запрос ждёт. По истечении времени возвращается 503, чтобы Codex мог повторить запрос.'],
  PROXY_RATE_LIMIT_BASE_S: ['Начальная пауза, с', 'Используется, когда провайдер не указал Retry-After.'],
  PROXY_RATE_LIMIT_MAX_DELAY_S: ['Максимум своей паузы, с', 'Не сокращает явную паузу, указанную провайдером.'],
  PROXY_MAX_RATE_LIMIT_WAIT_S: ['Бюджет восстановления, с', 'Если пауза не помещается, прокси возвращает 429 без раннего повтора.'],
  PROXY_MAX_TRANSIENT_RETRIES: ['Повторы временных ошибок', 'Максимальное число повторов восстановления.'],
  PROXY_RECOVERY_STAGGER_S: ['Интервал после 429, с', 'Разводит повторные запуски, чтобы не вызвать новый всплеск.'],
  PROXY_RATE_LIMIT_SCOPE: ['Разделение квот', 'По ключу — независимые ключи не задерживают друг друга.'],
  PROXY_RATE_LIMIT_PARTITIONS: ['Лимит независимых очередей', 'Активные очереди защищены от вытеснения.'],
  PROXY_SSE_BUFFER_BYTES: ['Буфер стрима, байт', 'Ограничение очереди одного ответа; память всего процесса больше.'],
  PROXY_SSE_PRIME_BYTES: ['Проверка начала стрима, байт', 'Предел буфера до передачи первых событий Codex.'],
  PROXY_SSE_PRIME_FRAMES: ['Проверка начала: событий', 'Предел числа служебных кадров до начала передачи.'],
  PROXY_SSE_PRIME_FIRST_TIMEOUT_S: ['Ожидание первого события, с', 'Время на первый кадр ответа провайдера.'],
  PROXY_SSE_PRIME_CREATED_GRACE_S: ['Пауза после создания, с', 'Короткая проверка ранних ошибок до выдачи ответа.'],
  PROXY_TIMINGS: ['Постоянный профиль', 'Для обычной работы достаточно временного профиля во вкладке «Диагностика».'],
  PROXY_HISTORY_ENABLED: ['История запросов', 'Сохраняет время, результат и объём передачи. Тексты запросов и ответов не записываются.'],
  PROXY_HISTORY_MAX_REQUESTS: ['Максимум запросов в истории', 'Старые записи удаляются автоматически. Страница показывает до 50 запросов.'],
  PROXY_HISTORY_HOURS: ['Срок хранения, часов', 'Ограничивает запросы, показатели и автоматические отчёты.'],
  PROXY_AUTO_DIAGNOSTICS: ['Диагностика при сбоях', 'После серии ошибок включает профиль на минуту и сохраняет сводный отчёт.'],
  PROXY_WATCHDOG: ['Восстанавливать зависания', 'Проверяет отзывчивость локального процесса. Обычное ожидание ответа провайдера не вызывает перезапуск.'],
  PROXY_WATCHDOG_INTERVAL_S: ['Интервал проверки зависания, с', 'Неудачные проверки должны повториться с этим интервалом.'],
  PROXY_WATCHDOG_FAILURES: ['Проверок перед восстановлением', 'Не меньше трёх. После запуска процессу даётся время на подготовку.'],
};

function Button({ children, className, tone = 'default', icon: Icon, ...props }) {
  return <button className={cn('button', `button-${tone}`, className)} {...props}>
    {Icon && <Icon size={16} strokeWidth={1.75} aria-hidden="true" />}{children}
  </button>;
}
function Dialog({ open, onOpenChange, title, description, children, accept, onAccept, danger = false, disabled = false }) {
  return <AlertDialog.Root open={open} onOpenChange={onOpenChange}>
    <AlertDialog.Portal><AlertDialog.Overlay className="fixed inset-0 z-40 bg-black/60" />
      <AlertDialog.Content className="panel fixed left-1/2 top-1/2 z-50 w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 p-6 shadow-xl">
        <AlertDialog.Title className="text-xl font-semibold">{title}</AlertDialog.Title>
        <AlertDialog.Description className="muted mt-3 text-sm leading-6">{description}</AlertDialog.Description>
        {children}<div className="mt-6 flex flex-wrap justify-end gap-2">
          <AlertDialog.Cancel asChild><Button>{t("Отмена")}</Button></AlertDialog.Cancel>
          <AlertDialog.Action asChild><Button tone={danger ? 'danger' : 'primary'} disabled={disabled} onClick={onAccept}>{accept}</Button></AlertDialog.Action>
        </div>
      </AlertDialog.Content>
    </AlertDialog.Portal>
  </AlertDialog.Root>;
}
function ErrorNote({ error }) {
  return error ? <p role="alert" className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">{t(explanations[error.code] || error.message || t("Не удалось выполнить действие. Попробуйте ещё раз."))}</p> : null;
}
function Sparkline({ values, label }) {
  const max = Math.max(1, ...values), width = 280, height = 48;
  const points = values.map((value, index) => `${index * width / Math.max(1, values.length - 1)},${height - 3 - value / max * (height - 6)}`).join(' ');
  return <svg viewBox={`0 0 ${width} ${height}`} className="mt-5 h-12 w-full text-emerald-600 dark:text-emerald-400" role="img" aria-label={t(label)}>
    <path d={`M0 ${height - 2}H${width}`} stroke="currentColor" opacity=".12" />
    {values.length > 1 && <polyline points={points} stroke="currentColor" strokeWidth="2" fill="none" strokeLinejoin="round" strokeLinecap="round" />}
  </svg>;
}
function ActionReceipt({ action }) {
  if (!action) return null;
  const label = action.state === 'rolled_back' ? t("Обновление отменено: предыдущая версия восстановлена")
    : action.state === 'verified' ? (action.action === 'update' ? t("Обновление установлено, работа прокси проверена") : t("Перезапуск завершён, работа прокси проверена"))
      : action.state === 'failed' ? t("Последнее действие не завершилось") : t("Прокси перезапускается");
  return <div role="status" className={cn('mt-5 rounded-lg px-3 py-2 text-sm',
    action.state === 'verified' ? 'bg-emerald-50 text-emerald-800 dark:bg-emerald-950/30 dark:text-emerald-200'
      : 'bg-amber-50 text-amber-800 dark:bg-amber-950/30 dark:text-amber-200')}>
    <p>{t(label)}{Number.isFinite(action.duration_s) ? ' · ' + elapsed(action.duration_s) : ''}</p>
    {action.state === 'failed' && <ErrorNote error={{ code: action.error_code }} />}
  </div>;
}

function Overview({ data, history, call, refresh, restart }) {
  const snapshot = data.snapshot, manage = data.management;
  const pool = snapshot.connection_pool, rate = snapshot.rate_limits, compression = snapshot.compression;
  const stats = snapshot.counts;
  const [pauseDialog, setPauseDialog] = useState(false), [error, setError] = useState(null), [working, setWorking] = useState(false);
  async function pause(value) {
    setWorking(true); setError(null);
    try { await call('pause', { paused: value }); await refresh(); } catch (e) { setError(e); } finally { setWorking(false); }
  }
  const workerOnline = data.supervisor?.worker_responsive !== false;
  const healthLabel = !workerOnline ? t("Прокси не отвечает") : manage.paused ? t("Новые запросы на паузе") : manage.draining ? t("Ожидание перезапуска") : snapshot.status === 'rate_limited' ? t("Ожидание квоты") : snapshot.status === 'busy' ? t("Очередь запросов") : t("Прокси готов");
  return <div className="space-y-5">
    <section className="panel p-5 sm:p-7">
      <div className="flex flex-wrap items-start justify-between gap-5">
        <div className="flex gap-4"><div className="flex size-12 shrink-0 items-center justify-center rounded-xl bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-400"><ShieldCheck size={26} strokeWidth={1.75} /></div>
          <div><div className="flex flex-wrap items-center gap-2"><h2 className="text-xl font-semibold">{healthLabel}</h2><span className="tag bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400">v{snapshot.version}</span></div>
            <p className="muted mt-1.5 text-sm">{t("Общий прокси для всех пользователей этого ПК.")}</p>
            <div className="muted mt-3 flex flex-wrap items-center gap-x-5 gap-y-2 text-xs"><span className="inline-flex items-center gap-1.5"><Clock3 size={14} /> {t("Работает")} {elapsed(snapshot.uptime_seconds)}</span><span className="mono">{window.location.host}</span></div>
          </div>
        </div>
        <div className="flex flex-wrap gap-2"><Button icon={manage.paused ? Play : Pause} disabled={!workerOnline || working || manage.draining} onClick={() => manage.paused ? pause(false) : setPauseDialog(true)}>{manage.paused ? t("Продолжить") : t("Пауза")}</Button>
          <Button icon={RefreshCw} onClick={restart} disabled={!workerOnline || !manage.restart_supported || manage.draining || updatePending(manage.update)}>{t("Перезапустить")}</Button></div>
      </div><ErrorNote error={error} />
      {manage.draining && <div className="mt-5 flex flex-wrap items-center justify-between gap-3 rounded-xl bg-amber-50 p-4 text-sm text-amber-800 dark:bg-amber-950/25 dark:text-amber-200"><span>{t("Ждём завершения")} {count(manage.active_requests)} {t("запросов. Новые временно приостановлены.")}</span><Button onClick={async () => { try { await call('restart/cancel', {}); await refresh(); } catch (e) { setError(e); } }}>{t("Отменить перезапуск")}</Button></div>}
      {manage.pending_settings && <p className="mt-5 rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:bg-amber-950/30 dark:text-amber-200">{t("Настройки сохранены. Перезапустите прокси, чтобы применить их.")}</p>}
      <ActionReceipt action={manage.last_action} />
      {manage.independent_panel_url && new URL(manage.independent_panel_url).origin !== location.origin && <a className="button button-default mt-5" href={manage.independent_panel_url}>{t("Открыть независимую панель")} <ArrowUpRight size={15} /></a>}
    </section>
    <section className="panel p-5 sm:p-6"><div className="flex flex-wrap items-center justify-between gap-4"><div><h2 className="font-semibold">{t("Память и история")}</h2><p className="muted mt-1 text-xs">{t("Память рабочего процесса измеряется раз в минуту.")}</p></div><a className="button button-default" href="#history">{t("История запросов")} <ChevronRight size={15} /></a></div><div className="mt-5 grid gap-5 sm:grid-cols-3"><div><p className="muted text-xs">{t("RAM процесса")}</p><p className="data mt-1 text-xl">{formatBytes(manage.history?.memory?.rss_bytes)}</p></div><div><p className="muted text-xs">{t("Выделенная память")}</p><p className="data mt-1 text-xl">{formatBytes(manage.history?.memory?.private_bytes)}</p></div><div><p className="muted text-xs">{t("Лимит истории")}</p><p className="data mt-1 text-xl">{count(manage.history?.max_requests)} {t("запросов")}</p><p className="muted mt-1 text-xs">{count(manage.history?.retention_hours)} {t("часов хранения")}</p></div></div>{manage.history?.dropped_records > 0 && <p role="status" className="muted mt-4 text-xs">{t("Пропущено записей истории:")} {count(manage.history.dropped_records)}{t(". Проверьте доступность диска.")}</p>}</section>
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
      {[{ title: t("Активные запросы"), value: manage.active_requests, suffix: '', icon: Activity, note: t("Включая ответы, которые ещё стримятся"), plot: history.map(x => x.active) },
        { title: t("Соединения"), value: pool.in_use, suffix: ` / ${pool.max_inflight}`, icon: Link2, note: t("{v0} в очереди · {v1} готовы к повтору", {v0: (count(pool.waiters)), v1: (count(pool.idle_connections))}), plot: history.map(x => x.inUse) },
        { title: t("После 429"), value: rate.queued_requests, suffix: '', icon: Timer, note: rate.cooling_partitions ? t("Пауза до {v0}", {v0: (elapsed(rate.longest_cooldown_remaining_s))}) : t("Сейчас нет задерживающей квоты"), plot: history.map(x => x.queued) },
        { title: t("Запросов с запуска"), value: stats.responses_requests, suffix: '', icon: Radio, note: t("{v0} восстановлены автоматически", {v0: (count(stats.successful_recoveries))}), plot: history.map(x => x.newRequests) }].map(item => <section key={item.title} className="panel p-5"><div className="muted flex items-center justify-between text-xs font-medium"><span>{item.title}</span><item.icon size={16} /></div><p className="data mt-4 text-3xl font-semibold">{count(item.value)}<span className="muted text-lg font-normal">{item.suffix}</span></p><p className="muted mt-2 min-h-8 text-xs leading-4">{item.note}</p><Sparkline values={item.plot} label={t("Последние значения: {v0}", {v0: (item.title)})} /></section>)}
    </div>
    <div className="grid gap-5 lg:grid-cols-3">
      <section className="panel p-5 sm:p-6 lg:col-span-2"><div className="flex flex-wrap items-center justify-between gap-3"><h2 className="font-semibold">{t("Что происходит сейчас")}</h2><span className="muted text-xs">{t("Обновляется каждые 2 секунды")}</span></div>
        {snapshot.findings.length ? <ul className="mt-5 divide-y divide-zinc-100 dark:divide-zinc-800">{snapshot.findings.map(item => <li key={item.code} className="flex items-start gap-3 py-3"><Info className="mt-0.5 shrink-0 text-amber-500" size={17} /><div><p className="text-sm leading-6">{t(findText[item.code] || item.message)}</p><span className="muted text-xs">{item.kind === 'current' ? t("Сейчас") : t("С момента запуска")}</span></div></li>)}</ul> : <div className="mt-6 flex items-center gap-4 rounded-xl bg-zinc-50 p-5 dark:bg-zinc-950/70"><CircleCheck className="text-emerald-500" size={24} /><div><p className="text-sm font-medium">{t("Очереди свободны")}</p><p className="muted mt-1 text-sm">{t("Признаков текущей блокировки нет. Провайдер может продолжать генерацию.")}</p></div></div>}
        <div className="mt-5 grid gap-4 border-t border-zinc-100 pt-5 dark:border-zinc-800 sm:grid-cols-3"><div><p className="muted text-xs">{t("Таймауты пула")}</p><p className="data mt-2 font-semibold">{count(stats.pool_timeouts)}</p></div><div><p className="muted text-xs">{t("Встречено 429")}</p><p className="data mt-2 font-semibold">{count(stats.rate_limits_caught)}</p></div><div><p className="muted text-xs">{t("Время ожидания квоты")}</p><p className="data mt-2 font-semibold">{elapsed(rate.wait_seconds_total)}</p><p className="muted mt-1 text-xs">{t("Сумма по всем запросам")}</p></div></div>
      </section>
      <section className="panel p-5 sm:p-6"><div className="flex items-center justify-between"><h2 className="font-semibold">{t("Передача данных")}</h2><Zap className="muted" size={17} /></div><div className="mt-6 space-y-5"><div className="flex justify-between text-sm"><span className="muted">{t("Сжатие gzip")}</span><span className={compression.enabled ? 'text-emerald-600 dark:text-emerald-400' : ''}>{compression.enabled ? t("Включено") : t("Выключено")}</span></div><div className="flex justify-between text-sm"><span className="muted">{t("Движок")}</span><span className="mono">{compression.backend === 'isal' ? 'ISA-L' : 'stdlib'}</span></div><div className="flex justify-between text-sm"><span className="muted">{t("Задач сжатия")}</span><span className="data">{count(compression.jobs_inflight)} / {count(compression.max_jobs)}</span></div><div className="flex justify-between text-sm"><span className="muted">{t("Очистка соединений")}</span><span>{pool.cleanup_active ? t("Включено") : t("Не активна")}</span></div></div><div className="mt-5 space-y-2 border-t border-zinc-100 pt-4 text-xs dark:border-zinc-800"><div className="flex justify-between gap-3"><span className="muted">{t("Резерв больших тел")}</span><span className="data">{formatBytes(snapshot.body_memory?.reserved_bytes || 0)} / {snapshot.body_memory?.limit_bytes ? formatBytes(snapshot.body_memory.limit_bytes) : t("Выключено")}</span></div><div className="flex justify-between"><span className="muted">{t("В очереди памяти")}</span><span className="data">{count(snapshot.body_memory?.waiters || 0)}</span></div></div><p className="muted mt-6 border-t border-zinc-100 pt-4 text-xs leading-5 dark:border-zinc-800">{t("Диагностика и панель не отправляют запросы модели. Ключи остаются в Codex.")}</p></section>
    </div>
    <Dialog open={pauseDialog} onOpenChange={setPauseDialog} title={t("Приостановить новые запросы?")} description={t("Текущие ответы продолжатся. Новые запросы получат временную ошибку до нажатия «Продолжить». Пауза действует до перезапуска прокси.")} accept={t("Поставить на паузу")} onAccept={() => pause(true)} />
  </div>;
}

function Diagnostics({ data, call, refresh }) {
  const [duration, setDuration] = useState(60), [working, setWorking] = useState(false), [error, setError] = useState(null);
  const snapshot = data.snapshot;
  const active = snapshot.timings_enabled;
  const measured = Object.values(snapshot.timings).some(stage => stage.count > 0 || stage.active > 0);
  async function profile(stop = false) {
    setWorking(true); setError(null);
    try { await call(stop ? 'profile/stop' : 'profile', stop ? {} : { duration_s: Number(duration) }); await refresh(); } catch (e) { setError(e); } finally { setWorking(false); }
  }
  const max = Math.max(1, ...Object.values(snapshot.timings).map(v => v.mean_ms || 0));
  return <div className="space-y-5">
    <section className="panel p-5 sm:p-7"><div className="flex flex-wrap items-start justify-between gap-6"><div><h2 className="text-xl font-semibold">{t("Профиль задержек")}</h2><p className="muted mt-2 max-w-xl text-sm leading-6">{t("Включите измерение, затем отправьте запрос в Codex. Будет видно, где проходит время: подготовка, соединение, квота или ожидание провайдера.")}</p></div><span className={cn('tag', active ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-400' : 'bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400')}><span className={cn('size-1.5 rounded-full', active ? 'bg-emerald-500' : 'bg-zinc-400')} />{active ? snapshot.timing_mode === 'always' ? t("Постоянный профиль") : t("Идёт запись · {v0}", {v0: (elapsed(snapshot.profile_remaining_s))}) : t("Измерение выключено")}</span></div>
      <div className="mt-6 flex flex-wrap items-center gap-3"><label htmlFor="profile-duration" className="label">{t("Длительность")}</label><select id="profile-duration" className="field w-auto" value={duration} onChange={e => setDuration(e.target.value)}>{[30, 60, 120, 300].map(v => <option key={v} value={v}>{v < 60 ? t("{v0} секунд", {v0: (v)}) : t("{v0} мин", {v0: (v / 60)})}</option>)}</select><Button tone="primary" icon={Play} disabled={working} onClick={() => profile()}>{active ? t("Продлить запись") : t("Начать измерение")}</Button>{active && snapshot.timing_mode !== 'always' && <Button icon={Pause} disabled={working} onClick={() => profile(true)}>{t("Остановить")}</Button>}<a className="button button-default sm:ml-auto" href="/_control/api/report" download><ArrowDownToLine size={16} />{t("Скачать отчёт")}</a></div><ErrorNote error={error} />
      <p className="muted mt-4 text-xs leading-5">{t("Выключится автоматически. Накопленные результаты останутся до перезапуска. Переписка и ключи не записываются.")}</p>
    </section>
    <section className="panel overflow-hidden"><div className="p-5 sm:p-6"><h2 className="font-semibold">{t("Этапы запроса")}</h2><p className="muted mt-1 text-xs">{t("Интервалы могут пересекаться. Их нельзя складывать или считать временем CPU прокси.")}</p></div>
      {!measured ? <div className="flex flex-col items-center px-5 py-14 text-center"><div className="mb-4 rounded-2xl bg-zinc-100 p-4 dark:bg-zinc-800"><Gauge size={28} className="muted" /></div><h3 className="font-medium">{t("Пока нет измерений")}</h3><p className="muted mt-2 max-w-md text-sm leading-6">{t("Запустите профиль и воспроизведите задержку. Обычный режим работает без подробных таймеров.")}</p><Button className="mt-5" tone="primary" disabled={working} onClick={() => profile()} icon={Play}>{t("Начать измерение")}</Button></div> : <div className="overflow-x-auto"><table className="w-full min-w-[640px] text-left text-sm"><thead className="bg-zinc-50 text-xs text-zinc-500 dark:bg-zinc-950/50 dark:text-zinc-400"><tr><th className="px-5 py-3 font-medium sm:px-6">{t("Этап")}</th><th className="px-3 py-3 font-medium">{t("Среднее")}</th><th className="px-3 py-3 font-medium">p95 ≤</th><th className="px-3 py-3 font-medium">{t("Замеров")}</th><th className="px-5 py-3 font-medium">{t("Сейчас")}</th></tr></thead><tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">{Object.entries(stages).map(([key, [label, explanation]]) => { const timing = snapshot.timings[key]; return <tr key={key}><td className="w-2/5 px-5 py-3 sm:px-6"><span className="font-medium">{t(label)}</span><p className="muted mt-0.5 text-xs">{t(explanation)}</p></td><td className="data px-3 py-3"><span>{millis(timing.mean_ms)}</span><div className="mt-1.5 h-1 w-24 overflow-hidden rounded-full bg-zinc-100 dark:bg-zinc-800"><div className="h-full bg-emerald-500/70" style={{ width: `${Math.max(0, (timing.mean_ms || 0) / max * 100)}%` }} /></div></td><td className="data px-3 py-3">{timing.count && timing.p95_bucket_upper_ms == null ? t("> 600 с") : millis(timing.p95_bucket_upper_ms)}</td><td className="data px-3 py-3">{count(timing.count)}</td><td className="data px-5 py-3">{timing.active ? <span className="tag bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-400">{count(timing.active)} {t("в работе")}</span> : <span className="muted">—</span>}</td></tr>; })}</tbody></table></div>}
      <p className="muted border-t border-zinc-100 px-5 py-4 text-xs leading-5 dark:border-zinc-800 sm:px-6">{t("p95 — верхняя граница корзины гистограммы, а не точный процентиль. Замеры включают ошибки и отмены, а сетевые этапы — ожидание провайдера.")}</p>
    </section>
  </div>;
}

function Settings({ call, restart, refresh, workerPid, restartBlocked }) {
  const [source, setSource] = useState(null), [draft, setDraft] = useState({}), [error, setError] = useState(null), [saved, setSaved] = useState(false), [working, setWorking] = useState(false);
  const load = useCallback(async () => { try { const value = await call(null, null, 'settings'); setSource(value); setDraft(value.saved); setError(null); setSaved(false); } catch (e) { setError(e); } }, [call]);
  useEffect(() => { load(); }, [load, workerPid]);
  const changes = source ? Object.fromEntries(Object.entries(draft).filter(([key, value]) => value !== source.saved[key])) : {};
  const dirty = Object.keys(changes).length > 0;
  async function save() {
    setWorking(true); setError(null);
    try { const next = await call('settings', { changes, revision: source.revision }); setSource({ ...source, saved: next.values, revision: next.revision, restart_required: next.restart_required }); setDraft(next.values); setSaved(true); await refresh(); } catch (e) { setError(e); } finally { setWorking(false); }
  }
  return <div className="space-y-5"><section className="panel p-5 sm:p-6"><div className="flex flex-wrap items-start justify-between gap-5"><div><h2 className="text-xl font-semibold">{t("Настройки прокси")}</h2><p className="muted mt-2 text-sm leading-6">{t("Сохраняются для всех пользователей этого ПК. Применяются после перезапуска; настройки Codex и ключи остаются на месте.")}</p></div><Button icon={RefreshCw} onClick={load}>{t("Обновить значения")}</Button></div><ErrorNote error={error} /></section>
    {!source ? <div className="panel p-6"><p className="muted">{t("Загружаем настройки…")}</p></div> : <>
      {Object.entries({ compression: [t("Передача и сжатие"), Zap], recovery: [t("Восстановление после ошибок"), ShieldCheck], network: [t("Подключение"), Link2], streaming: [t("Стриминг"), Waves], diagnostics: [t("Диагностика"), Gauge], history: [t("Хранение истории"), Clock3] }).map(([group, [title, Icon]]) => <section key={group} className="panel p-5 sm:p-6"><h3 className="flex items-center gap-2.5 font-semibold"><Icon size={18} className="muted" />{title}</h3><div className="mt-6 grid gap-x-8 gap-y-6 md:grid-cols-2">{Object.entries(source.schema).filter(([, spec]) => spec.group === group).map(([name, spec]) => { const [label, help] = settingsCopy[name] || [name, '']; const invalid = error?.fields?.includes(name); return <div key={name} className={spec.kind === 'bool' ? 'md:col-span-2' : ''}>{spec.kind === 'bool' ? <label className="flex cursor-pointer items-center justify-between gap-4 rounded-xl bg-zinc-50 p-4 dark:bg-zinc-950/60"><div><span className="label">{t(label)}</span><p className="muted mt-1 text-xs leading-5">{t(help)}</p></div><input aria-label={t(label)} type="checkbox" className="size-5 shrink-0 accent-emerald-600" checked={Boolean(draft[name])} onChange={e => { setDraft({ ...draft, [name]: e.target.checked }); setSaved(false); }} /></label> : <><label className="label" htmlFor={name}>{t(label)}</label>{spec.kind === 'enum' ? <select id={name} className="field mt-2" value={draft[name]} onChange={e => { setDraft({ ...draft, [name]: e.target.value }); setSaved(false); }}>{spec.values.map(value => <option key={value} value={value}>{value === 'credential' ? t("По ключу") : t("Общая квота")}</option>)}</select> : <input id={name} type="number" min={spec.min} max={spec.max} step={spec.kind === 'int' ? 1 : 'any'} value={draft[name] ?? ''} aria-invalid={invalid || undefined} className={cn('field data mt-2', invalid && 'border-red-500')} onChange={e => { setDraft({ ...draft, [name]: e.target.value === '' ? '' : Number(e.target.value) }); setSaved(false); }} />}<p className="muted mt-1.5 text-xs leading-5">{t(help)}</p></>}</div>; })}</div></section>)}
      <section className="panel p-5 sm:p-6"><div className="flex flex-wrap items-center justify-between gap-4"><div><p className="text-sm font-medium">{dirty ? t("Изменено настроек: {v0}", {v0: (Object.keys(changes).length)}) : saved ? t("Настройки сохранены") : t("Изменений нет")}</p><p className="muted mt-1 text-xs">{source.restart_required || dirty ? t("Перезапуск применит сохранённые значения.") : t("Текущая версия уже использует эти значения.")}</p></div><div className="flex flex-wrap gap-2"><Button disabled={!dirty || working} onClick={() => { setDraft(source.saved); setSaved(false); }}>{t("Сбросить изменения")}</Button><Button tone="primary" icon={Check} disabled={!dirty || working} onClick={save}>{working ? t("Сохраняем…") : t("Сохранить")}</Button>{source.restart_required && !dirty && <Button icon={RefreshCw} onClick={restart} disabled={restartBlocked}>{t("Применить перезапуском")}</Button>}</div></div><ErrorNote error={error} /></section>
    </>}
  </div>;
}

function Updates({ data, call, refresh, onInstalling }) {
  const [working, setWorking] = useState(null), [error, setError] = useState(null), [installDialog, setInstallDialog] = useState(false);
  const [accepted, setAccepted] = useState(null);
  const observed = data.management.update;
  useEffect(() => {
    if (accepted && (observed.job_id === accepted.job_id || data.snapshot.process_id !== accepted.workerPid)) setAccepted(null);
  }, [accepted, observed.job_id, data.snapshot.process_id]);
  const update = accepted && observed.job_id !== accepted.job_id ? accepted : observed;
  const pending = updatePending(update);
  const busy = Boolean(working) || pending;
  const jobError = update.state === 'failed' ? { code: update.error_code } : null;
  async function check() { setWorking('check'); setError(null); try { const result = await call('updates/check', {}); if (result.state === 'failed') throw { code: result.error_code }; if (result.job_id) setAccepted({ ...result, workerPid: data.snapshot.process_id }); await refresh(); } catch (e) { setError(e); } finally { setWorking(null); } }
  async function prepare() { setWorking('prepare'); setError(null); try { const result = await call('updates/prepare', { commit: update.commit }); if (result.state === 'failed') throw { code: result.error_code }; if (result.job_id) setAccepted({ ...result, workerPid: data.snapshot.process_id }); await refresh(); } catch (e) { setError(e); } finally { setWorking(null); } }
  async function install() { setWorking('install'); setError(null); try { await call('updates/install', { commit: update.commit }); onInstalling(); await refresh(); } catch (e) { setError(e); } finally { setWorking(null); } }
  return <div className="space-y-5"><section className="panel p-5 sm:p-7"><div className="flex flex-wrap items-start justify-between gap-5"><div className="flex gap-4"><div className="flex size-12 shrink-0 items-center justify-center rounded-xl bg-zinc-100 dark:bg-zinc-800"><PackageCheck size={25} className="text-emerald-600 dark:text-emerald-400" /></div><div><h2 className="text-xl font-semibold">{t("Обновления")}</h2><p className="muted mt-2 text-sm">{t("Установлена версия")} <span className="data font-medium text-zinc-800 dark:text-zinc-200">{data.snapshot.version}</span></p><p className="muted mt-1 text-xs">zerkavich / agentrouter-codex-proxy · main</p></div></div><Button icon={RefreshCw} tone="primary" onClick={check} disabled={busy || data.management.draining}>{working === 'prepare' || update.state === 'preparing' ? t("Подготавливаем пакет…") : working === 'install' ? t("Перезапускаем…") : busy ? t("Проверяем GitHub…") : t("Проверить обновления")}</Button></div><ErrorNote error={error || jobError} />
      {pending && <div role="status" aria-live="polite" className="mt-6 rounded-xl bg-zinc-50 p-5 dark:bg-zinc-950/70"><p className="flex items-center gap-2.5 font-medium"><RefreshCw size={17} className="animate-spin motion-reduce:animate-none" />{update.state === 'preparing' ? t("Подготавливаем пакет…") : t("Проверяем GitHub…")}</p><p className="muted mt-2 text-sm leading-6">{t("Можно закрыть вкладку. Результат появится здесь после завершения.")}</p></div>}
      {(['checked', 'prepared'].includes(update.state) || (update.state === 'failed' && update.commit && update.update_available)) && <div className="mt-6 rounded-xl bg-zinc-50 p-5 dark:bg-zinc-950/70"><h3 className="font-medium">{update.state === 'prepared' ? t("Пакет {v0} проверен и готов", {v0: (update.latest_version)}) : update.update_available ? t("Доступно обновление{v0}", {v0: (update.latest_version ? ` ${update.latest_version}` : '')}) : t("Установлена актуальная версия")}</h3><p className="muted mt-2 text-sm leading-6">{update.title}</p><div className="mt-4 flex flex-wrap gap-3">{update.install_supported && update.update_available && <Button tone="primary" disabled={busy || data.management.draining} onClick={update.state === 'prepared' ? () => setInstallDialog(true) : prepare} icon={PackageCheck}>{working ? t("Подготавливаем…") : update.state === 'prepared' ? t("Установить обновление") : update.state === 'failed' ? t("Повторить подготовку") : t("Подготовить обновление")}</Button>}<a className="button button-default" href={update.url} target="_blank" rel="noreferrer">{t("Изменения")} <ArrowUpRight size={15} /></a><a className="button button-default" href="https://github.com/zerkavich/agentrouter-codex-proxy/blob/main/docs/windows-installation.md" target="_blank" rel="noreferrer">{t("Инструкция установки")} <ArrowUpRight size={15} /></a></div>{update.install_supported && <p className="muted mt-4 text-xs leading-5">{t("Подготовка проверяет четыре CI-задачи и версии зависимостей. При установке сохраняется резервная копия; ошибка запуска вызывает откат.")}</p>}</div>}
      {update.state === 'not_checked' && <p className="muted mt-6 rounded-xl bg-zinc-50 p-5 text-sm leading-6 dark:bg-zinc-950/70">{t("Проверка выполняется по нажатию. Панель не обращается к GitHub в фоне.")}</p>}
    </section><section className="panel p-5 sm:p-6"><h2 className="font-semibold">{t("Изменения этой версии")}</h2><ul className="muted mt-4 space-y-3 text-sm leading-6"><li className="flex gap-3"><Check size={17} className="mt-1 shrink-0 text-emerald-500" />{t("Управление, состояние и профиль прямо в браузере.")}</li><li className="flex gap-3"><Check size={17} className="mt-1 shrink-0 text-emerald-500" />{t("Пауза новых запросов и перезапуск после завершения текущих.")}</li><li className="flex gap-3"><Check size={17} className="mt-1 shrink-0 text-emerald-500" />{t("Настройки для всех пользователей отдельно от ключей и конфигурации Codex.")}</li><li className="flex gap-3"><Check size={17} className="mt-1 shrink-0 text-emerald-500" />{t("Экспорт отчёта без переписки и учётных данных.")}</li></ul></section><Dialog open={installDialog} onOpenChange={setInstallDialog} title={t("Установить {v0}?", {v0: (update.latest_version || t("обновление"))})} description={t("Дождёмся завершения текущих запросов, сохраним предыдущую версию и перезапустим прокси с новым пакетом. Панель переподключится. Ключи и история Codex сохраняются.")} accept={t("Установить и перезапустить")} onAccept={install} /></div>;
}

const eventCopy = {
  worker_started: 'Рабочий процесс запущен', worker_exit: 'Рабочий процесс завершился',
  watchdog_unresponsive: 'Проверка: процесс не отвечает', watchdog_restart: 'Автоматическое восстановление',
  watchdog_recovered: 'Процесс снова отвечает', watchdog_suppressed: 'Достигнут лимит автоматических восстановлений',
  auto_profile_started: 'Начата автоматическая диагностика', auto_profile_saved: 'Автоматический отчёт сохранён',
  manual_recovery: 'Восстановление из панели', history_write_failed: 'Ошибка записи истории', history_cleared: 'История очищена',
};
const outcomeCopy = { active: 'В работе', success: 'Успешно', http_error: 'Ошибка HTTP', cancelled: 'Отменён', interrupted: 'Обрыв' };
const attemptCopy = {
  upstream: 'Ответ провайдера', transport_connect: 'Сбой подключения', transport_timeout: 'Таймаут передачи',
  transport_protocol: 'Соединение прервано', transport_pool: 'Ожидание соединения истекло', transport_other: 'Ошибка передачи',
  retry_wait: 'Пауза перед повтором', quota_wait: 'Пауза квоты', resource_recovery: 'Восстановление истории',
  gzip_recovery: 'Повтор без gzip', sse_error: 'Ошибка в начале стрима', memory_wait: 'Очередь памяти',
  memory_budget_exhausted: 'Ожидание памяти истекло', binding_invalidated: 'Привязка сессии недоступна', cancelled: 'Отменён',
};
function RequestAttempts({ call, row, language }) {
  const [open, setOpen] = useState(false), [result, setResult] = useState(null), [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    if (!open || row.outcome === 'active') return;
    const controller = new AbortController();
    setLoading(true); setError(null);
    call(null, null, 'history/attempts/' + row.id, controller.signal).then(value => {
      if (!controller.signal.aborted) setResult(value);
    }).catch(value => { if (!controller.signal.aborted) setError(value); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [open, row.id, row.outcome, call]);
  return <details className="mt-2 max-w-sm" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary className="cursor-pointer rounded text-xs text-emerald-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 dark:text-emerald-400">{t("Попытки и восстановление")}</summary>
    {open && <div className="mt-3 rounded-xl bg-zinc-50 p-3 dark:bg-zinc-950/60" aria-busy={loading}>
      {row.outcome === 'active' ? <p className="muted text-xs" role="status">{t("Детали появятся после завершения запроса.")}</p>
        : loading ? <p className="muted text-xs" role="status">{t("Загружаем попытки…")}</p>
        : error ? <ErrorNote error={error} />
        : result?.events?.length ? <><ol className="space-y-3">{result.events.map(([code, status, duration, wait], index) => <li key={index} className="text-xs leading-5">
          <span className="font-medium">{t(attemptCopy[code] || 'Ошибка передачи')}</span>
          <p className="data muted">{status ? `HTTP ${status} · ` : ''}{millis(duration)}{wait > 0 ? <> · {t("Пауза:")} {millis(wait)}</> : null}</p>
          {code === 'binding_invalidated' && <p className="mt-1 text-amber-700 dark:text-amber-300">{t("Шлюз больше не обслуживает привязку этой сессии. Начните новую задачу Codex; файлы проекта сохранятся.")}</p>}
        </li>)}</ol>{result.truncated && <p className="muted mt-3 text-xs">{t("Показаны последние 16 событий.")}</p>}</>
        : <p className="muted text-xs">{t("Для этого запроса детали попыток не сохранены.")}</p>}
    </div>}
  </details>;
}
const HistoryPage = React.memo(function HistoryPage({ call, language, enabled }) {
  const [view, setView] = useState('requests'), [filter, setFilter] = useState(''), [cursor, setCursor] = useState(null);
  const [pages, setPages] = useState([]), [result, setResult] = useState(null);
  const [error, setError] = useState(null), [loading, setLoading] = useState(false), [clearDialog, setClearDialog] = useState(false);
  const flight = useRef(null);
  const load = useCallback(async signal => {
    const key = view + ':' + filter + ':' + cursor;
    if (flight.current?.key === key) return flight.current.promise;
    flight.current?.controller.abort();
    const controller = new AbortController();
    const activeSignal = signal ? AbortSignal.any([controller.signal, signal]) : controller.signal;
    const activeFlight = { key, controller, promise: null };
    flight.current = activeFlight;
    setLoading(true);
    const query = view === 'requests' ? `?limit=50${filter ? `&outcome=${encodeURIComponent(filter)}` : ''}${cursor ? `&before=${cursor}` : ''}` : '';
    activeFlight.promise = (async () => {
      try {
        const [value, reportList] = await Promise.all([
          call(null, null, 'history/' + view + query, activeSignal),
          view === 'events' ? call(null, null, 'history/reports', activeSignal) : Promise.resolve({ items: [] }),
        ]);
        if (!activeSignal.aborted) { setResult({ key, value, reports: reportList.items }); setError(null); }
      } catch (e) { if (!activeSignal.aborted) setError(e); }
      finally { if (flight.current === activeFlight) flight.current = null; if (!activeSignal.aborted) setLoading(false); }
    })();
    return activeFlight.promise;
  }, [call, cursor, filter, view]);
  useEffect(() => {
    const controller = new AbortController();
    let timer, stopped = false;
    setResult(null);
    const poll = async () => { if (!document.hidden) await load(controller.signal); if (!stopped) timer = setTimeout(poll, 5000); };
    poll();
    const visible = () => { if (!document.hidden) load(controller.signal); };
    document.addEventListener('visibilitychange', visible);
    return () => { stopped = true; controller.abort(); clearTimeout(timer); document.removeEventListener('visibilitychange', visible); };
  }, [load]);
  const current = result?.key === view + ':' + filter + ':' + cursor ? result.value : null;
  const items = current?.items || [], reports = current ? result.reports : [];
  const graphItems = React.useMemo(() => {
    if (view !== 'metrics') return [];
    const samples = [], step = Math.max(1, Math.ceil(items.length / 120));
    for (let index = 0; index < items.length; index += step) {
      const block = items.slice(index, index + step);
      samples.push({ active: Math.max(0, ...block.map(row => row.active)), queued: Math.max(0, ...block.map(row => row.queued)),
        memory: Math.max(0, ...block.map(row => (row.rss_bytes || 0) / 1048576)) });
    }
    return samples;
  }, [current, view]);
  async function clearHistory() {
    try { await call('history/clear', { confirm: true }); setCursor(null); setPages([]); await load(); } catch (e) { setError(e); }
  }
  const switchView = next => { setView(next); setCursor(null); setPages([]); };
  return <div className="space-y-5"><section className="panel p-5 sm:p-6"><div className="flex flex-wrap items-start justify-between gap-4"><div><h2 className="text-xl font-semibold">{t("История работы")}</h2><p className="muted mt-2 max-w-2xl text-sm leading-6">{t("Запросы, память, очереди и восстановление сохраняются между перезапусками. Тексты запросов, ответы и ключи не записываются.")}</p></div><Button icon={RefreshCw} disabled={loading} onClick={() => load()}>{t("Обновить историю")}</Button></div><div className="mt-5 flex flex-wrap gap-2">{[['requests', t("Запросы")], ['metrics', t("Показатели за сутки")], ['events', t("Сбои и отчёты")]].map(([id, label]) => <Button key={id} aria-pressed={view === id} tone={view === id ? 'primary' : 'default'} onClick={() => switchView(id)}>{t(label)}</Button>)}</div>{!enabled && <p role="status" className="muted mt-4 text-sm">{t("Запись истории выключена в настройках. Сохранённые записи остаются доступны.")}</p>}<ErrorNote error={error} /></section>
    {view === 'requests' ? <section className="panel overflow-hidden"><div className="flex flex-wrap items-center justify-between gap-3 p-5 sm:px-6"><label className="flex items-center gap-3 text-sm" htmlFor="request-filter">{t("Результат")}<select id="request-filter" className="field w-auto" value={filter} onChange={e => { setFilter(e.target.value); setCursor(null); setPages([]); }}><option value="">{t("Все запросы")}</option>{Object.entries(outcomeCopy).map(([id, label]) => <option key={id} value={id}>{t(label)}</option>)}</select></label><span className="muted text-xs">{count(current?.max_requests || 2000)} {t("записей ·")} {count(current?.retention_hours || 24)} {t("часов")}</span></div>
      {items.length ? <div className="overflow-x-auto"><table className="w-full min-w-[850px] text-left text-sm"><thead className="bg-zinc-50 text-xs text-zinc-500 dark:bg-zinc-950/50 dark:text-zinc-400"><tr>{[t("Время"), t("Запрос"), t("Результат"), t("Весь ответ"), t("До заголовков"), t("Ожидание")].map(label => <th key={t(label)} className="px-4 py-3 font-medium first:pl-6">{t(label)}</th>)}</tr></thead><tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">{items.map(row => <tr key={row.id}><td className="data whitespace-nowrap py-4 pl-6 pr-4 text-xs">{dateTime(row.started_at)}</td><td className="px-4 py-4"><span className="mono text-xs">{row.method} /{row.endpoint}</span><p className="muted mt-1 text-xs">{formatBytes(row.request_bytes)} → {formatBytes(row.response_bytes)}</p><RequestAttempts call={call} row={row} language={language} /></td><td className="px-4 py-4"><span className={cn('tag', row.outcome === 'success' ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-400' : row.outcome === 'cancelled' ? 'bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400' : 'bg-amber-50 text-amber-700 dark:bg-amber-950/50 dark:text-amber-300')}>{t(outcomeCopy[row.outcome] || row.outcome)}</span><p className="data muted mt-1 text-xs">HTTP {row.status || '—'}</p></td><td className="data px-4 py-4">{millis(row.outcome === 'active' ? Date.now() - row.started_at * 1000 : row.duration_ms)}</td><td className="data px-4 py-4">{millis(row.headers_ms)}</td><td className="data px-4 py-4 text-xs"><p>{t("Квота:")} {millis(row.quota_ms)}</p><p className="muted mt-1">{t("Соединение:")} {millis(row.admission_ms)}</p><p className="muted mt-1">{t("Подготовка:")} {millis(row.prepare_ms)}</p></td></tr>)}</tbody></table></div> : <p className="muted px-6 py-12 text-center text-sm">{loading ? t("Загружаем историю…") : t("Запросов с таким результатом пока нет.")}</p>}
      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-zinc-100 p-5 dark:border-zinc-800 sm:px-6"><p className="muted max-w-md text-xs leading-5">{t("Длительность включает передачу всего ответа. Ожидание показывается по отдельным этапам.")}</p><div className="flex gap-2"><Button disabled={!pages.length || loading} onClick={() => { const previous = pages[pages.length - 1]; setPages(old => old.slice(0, -1)); setCursor(previous); }}>{t("Новее")}</Button><Button disabled={!current?.next_before || loading} onClick={() => { setPages(old => [...old.slice(-199), cursor]); setCursor(current.next_before); }}>{t("Старее")}</Button></div></div></section>
      : view === 'metrics' ? <section className="panel p-5 sm:p-6"><h2 className="font-semibold">{t("Память и нагрузка за сутки")}</h2><p className="muted mt-2 text-xs leading-5">{t("До 1440 сохранённых точек. График показывает максимумы в каждом интервале; в браузере не больше 120 точек.")}</p>{graphItems.length ? <><p className="muted mt-4 text-xs">{dateTime(items[0].sampled_at)} — {dateTime(items[items.length - 1].sampled_at)}</p><div className="mt-6 grid gap-6 sm:grid-cols-3">{[['memory', t("RAM, МиБ")], ['active', t("Активные запросы")], ['queued', t("Очередь квоты")]].map(([key, label]) => <div key={key}><p className="label">{t(label)}</p><p className="data mt-2 text-2xl">{count(Math.max(0, ...graphItems.map(row => row[key])))}</p><Sparkline label={t(label)} values={graphItems.map(row => row[key])} /></div>)}</div></> : <p className="muted py-12 text-center text-sm">{t("Показатели появятся после запуска рабочего процесса.")}</p>}</section>
      : <><section className="panel p-5 sm:p-6"><h2 className="font-semibold">{t("Автоматические отчёты")}</h2><p className="muted mt-2 text-sm">{t("После серии ошибок сохраняется краткий профиль задержек. Храним до 16 отчётов.")}</p>{reports.length ? <div className="mt-5 flex flex-wrap gap-3">{reports.map(row => <a key={row.id} href={`/_control/api/history/report/${row.id}`} download className="button button-default"><ArrowDownToLine size={15} />{dateTime(row.captured_at)}</a>)}</div> : <p className="muted mt-5 text-sm">{t("Автоматических отчётов пока нет.")}</p>}</section><section className="panel p-5 sm:p-6"><h2 className="font-semibold">{t("События сервиса")}</h2>{items.length ? <ul className="mt-5 divide-y divide-zinc-100 dark:divide-zinc-800">{items.map(row => <li key={row.id} className="flex flex-wrap items-start justify-between gap-3 py-4 text-sm"><span>{t(eventCopy[row.code] || row.code)}</span><time className="data muted text-xs" dateTime={new Date(row.occurred_at * 1000).toISOString()}>{dateTime(row.occurred_at)}</time></li>)}</ul> : <p className="muted mt-5 text-sm">{t("Событий пока нет.")}</p>}</section></>}
    <div className="flex justify-end"><Button disabled={!enabled} onClick={() => setClearDialog(true)}>{t("Очистить историю")}</Button></div><Dialog open={clearDialog} onOpenChange={setClearDialog} title={t("Очистить историю прокси?")} description={t("Будут удалены сохранённые записи запросов, показатели и автоматические отчёты. История чатов Codex сохраняется.")} accept={t("Очистить историю")} danger onAccept={clearHistory} />
  </div>;
});

function App() {
  const [tab, setTab] = useState(() => ['diagnostics', 'settings', 'updates', 'history'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'overview');
  const [theme, setTheme] = useState(() => { try { return localStorage.getItem('proxy-panel-theme') || 'dark'; } catch { return 'dark'; } });
  const [data, setData] = useState(null), [history, setHistory] = useState([]), [connected, setConnected] = useState(true), [initialError, setInitialError] = useState(null);
  const [restartDialog, setRestartDialog] = useState(false), [restartForce, setRestartForce] = useState(false), [restartError, setRestartError] = useState(null), [restarting, setRestarting] = useState(false);
  const [lastSeen, setLastSeen] = useState(null), [refreshing, setRefreshing] = useState(false);
  const [language, setLanguageState] = useState(getLanguage);
  const [recoverDialog, setRecoverDialog] = useState(false);
  const token = useRef(null), sessionPromise = useRef(null), previous = useRef(null), statusFlight = useRef(null), disposed = useRef(false);
  useEffect(() => { document.documentElement.classList.toggle('dark', theme === 'dark'); try { localStorage.setItem('proxy-panel-theme', theme); } catch {} }, [theme]);
  const session = useCallback(async () => {
    if (token.current) return token.current;
    if (!sessionPromise.current) sessionPromise.current = fetch('/_control/api/session', { cache: 'no-store', signal: AbortSignal.timeout(4000) }).then(async response => {
      const result = await response.json(); if (!response.ok) throw { code: result.error_code }; token.current = result.token; return result.token;
    }).finally(() => { sessionPromise.current = null; });
    return sessionPromise.current;
  }, []);
  const call = useCallback(async (action, value, read = null, requestSignal = null) => {
    const controlToken = read ? null : await session();
    const timeoutSignal = AbortSignal.timeout(action?.startsWith('updates/') ? 90000 : 7000);
    const response = await fetch('/_control/api/' + (read || action), {
      method: read ? 'GET' : 'POST', cache: 'no-store',
      headers: read ? {} : { 'Content-Type': 'application/json', 'X-Proxy-Control': controlToken },
      body: read ? undefined : JSON.stringify(value), signal: requestSignal ? AbortSignal.any([timeoutSignal, requestSignal]) : timeoutSignal,
    });
    const result = await response.json();
    if (!response.ok) { if (result.error_code === 'panel_session_expired') token.current = null; throw { code: result.error_code, fields: result.fields }; }
    return result;
  }, [session]);
  const refresh = useCallback(async () => {
    if (statusFlight.current) return statusFlight.current;
    statusFlight.current = (async () => {
      try {
        const response = await fetch('/_control/api/status', { cache: 'no-store', signal: AbortSignal.timeout(4000) });
        const result = await response.json();
        if (!response.ok) throw { code: result.error_code };
        if (disposed.current) return;
        if (previous.current && previous.current.pid !== result.snapshot.process_id) { token.current = null; setHistory([]); setRestarting(false); }
        const requests = result.snapshot.counts.responses_requests || 0;
        const sample = { active: result.management.active_requests, inUse: result.snapshot.connection_pool.in_use,
          queued: result.snapshot.rate_limits.queued_requests, newRequests: previous.current && previous.current.pid === result.snapshot.process_id ? Math.max(0, requests - previous.current.requests) : 0 };
        previous.current = { pid: result.snapshot.process_id, requests };
        setHistory(old => [...old.slice(-44), sample]); setData(result); setConnected(true); setInitialError(null); setLastSeen(new Date());
      } catch (error) {
        if (!disposed.current) { setConnected(false); setInitialError(error); }
      } finally { statusFlight.current = null; }
    })();
    return statusFlight.current;
  }, []);
  useEffect(() => {
    disposed.current = false;
    let timer;
    const poll = async () => { if (!document.hidden) await refresh(); if (!disposed.current) timer = setTimeout(poll, document.hidden ? 10000 : 2000); };
    poll();
    const visible = () => { if (!document.hidden) refresh(); };
    document.addEventListener('visibilitychange', visible);
    return () => { disposed.current = true; clearTimeout(timer); document.removeEventListener('visibilitychange', visible); };
  }, [refresh]);
  useEffect(() => { const handler = () => setTab(['diagnostics', 'settings', 'updates', 'history'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'overview'); addEventListener('hashchange', handler); return () => removeEventListener('hashchange', handler); }, []);
  useEffect(() => { document.documentElement.lang = language; document.title = language === 'en' ? 'AgentRouter · Proxy control' : t("AgentRouter · Управление прокси"); }, [language]);
  async function restart() {
    setRestartError(null);
    try { await call('restart', { force: restartForce }); setRestarting(true); await refresh(); } catch (error) { setRestartError(error); }
  }
  const tabs = [{ id: 'overview', label: t("Обзор"), icon: Activity }, { id: 'history', label: t("История"), icon: Clock3 }, { id: 'diagnostics', label: t("Диагностика"), icon: Gauge }, { id: 'settings', label: t("Настройки"), icon: Settings2 }, { id: 'updates', label: t("Обновления"), icon: PackageCheck }];
  const openRestart = () => { setRestartForce(false); setRestartDialog(true); };
  return <div className="min-h-dvh"><a href="#main" className="sr-only z-50 rounded-lg bg-emerald-600 p-3 text-white focus:not-sr-only focus:fixed focus:left-4 focus:top-4">{t("Перейти к содержимому")}</a>
    <header className="border-b border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-950"><div className="mx-auto flex max-w-7xl items-center justify-between gap-3 px-4 py-4 sm:px-8"><a href="#overview" className="flex items-center gap-3"><span className="flex size-10 items-center justify-center rounded-xl bg-zinc-900 text-emerald-400 dark:bg-zinc-800"><Code2 size={23} strokeWidth={1.75} /></span><span><span className="block text-sm font-semibold sm:text-base">AgentRouter</span><span className="muted block text-xs">Codex Proxy</span></span></a><div className="flex items-center gap-3"><label htmlFor="panel-language" className="sr-only">{t("Язык интерфейса")}</label><select id="panel-language" className="field w-auto text-sm" value={language} onChange={e => { setLanguage(e.target.value); setLanguageState(e.target.value); }}><option value="ru">{t("Русский")}</option><option value="en">English</option></select><Button icon={theme === 'dark' ? Sun : Moon} aria-label={theme === 'dark' ? t("Светлая тема") : t("Тёмная тема")} className="size-10 p-0" onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')} /></div></div></header>
    <div className="mx-auto max-w-7xl px-4 pb-12 pt-7 sm:px-8 sm:pt-10"><div className="mb-7 flex flex-wrap items-start justify-between gap-4"><div><p className="muted mb-2 text-xs font-medium">{t("УПРАВЛЕНИЕ ПРОКСИ")}</p><h1 className="text-2xl font-semibold sm:text-3xl">{tabs.find(item => item.id === tab)?.label}</h1></div><div className="flex items-center gap-3 pt-1"><span className={cn('tag', connected && data ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-400' : 'bg-amber-50 text-amber-700 dark:bg-amber-950/50 dark:text-amber-300')}><span className={cn('size-1.5 rounded-full', connected && data ? 'bg-emerald-500' : 'bg-amber-500')} />{connected && data ? t("Подключено") : restarting ? t("Перезапуск") : t("Подключение")}</span><Button icon={RefreshCw} aria-label={t("Обновить состояние")} className="size-10 p-0" disabled={refreshing} onClick={async () => { setRefreshing(true); await refresh(); setRefreshing(false); }} /></div></div>
      <nav aria-label={t("Разделы управления")} className="mb-7 flex gap-1 overflow-x-auto border-b border-zinc-200 dark:border-zinc-800">{tabs.map(({ id, label, icon: Icon }) => <a key={id} href={`#${id}`} aria-current={tab === id ? 'page' : undefined} className={cn('mb-[-1px] inline-flex min-h-11 shrink-0 items-center gap-2 border-b-2 px-3 py-3 text-sm font-medium sm:px-4', tab === id ? 'border-emerald-500 text-emerald-700 dark:text-emerald-400' : 'border-transparent text-zinc-500 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100')}><Icon size={17} strokeWidth={1.75} />{t(label)}</a>)}</nav>
      {!connected && data && <div role="status" className="mb-5 flex items-start gap-3 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200"><WifiOff size={18} className="mt-0.5 shrink-0" /><div><p className="font-medium">{restarting ? t("Прокси перезапускается") : t("Связь с прокси прервалась")}</p><p className="mt-1 leading-6">{t("Панель переподключится автоматически. Последние значения")} {lastSeen ? t("получены в {v0}", {v0: (lastSeen.toLocaleTimeString(locale()))}) : t("сохранены")}.</p></div></div>}
      {data?.supervisor?.worker_responsive === false && <section role="status" className="panel mb-5 border-amber-300 p-5 dark:border-amber-800"><div className="flex flex-wrap items-start justify-between gap-4"><div><h2 className="font-semibold">{t("Обработчик запросов не отвечает")}</h2><p className="muted mt-2 max-w-xl text-sm leading-6">{t("Панель продолжает работать. Сохранённая история доступна; можно восстановить прокси без перезагрузки ПК.")}</p>{data.supervisor.last_seen_at && <p className="muted mt-2 text-xs">{t("Последние показатели:")} {dateTime(Date.parse(data.supervisor.last_seen_at) / 1000)}</p>}</div><Button icon={RefreshCw} tone="primary" onClick={() => setRecoverDialog(true)}>{t("Восстановить прокси")}</Button></div></section>}
      <main id="main" tabIndex={-1}>{!data ? <section className="panel p-6 sm:p-10"><h2 className="font-semibold">{connected ? t("Подключаемся к прокси…") : t("Прокси пока недоступен")}</h2><p className="muted mt-3 text-sm leading-6">{connected ? t("Загружаем локальное состояние.") : t("Проверьте, что сервис запущен. Эта страница попробует подключиться снова.")}</p>{!connected && <ErrorNote error={initialError} />}<div className="mt-6 grid gap-4 sm:grid-cols-3">{[1, 2, 3].map(i => <div key={i} className="h-24 rounded-xl bg-zinc-100 dark:bg-zinc-800" />)}</div></section> : tab === 'overview' ? <Overview data={data} history={history} call={call} refresh={refresh} restart={openRestart} /> : tab === 'history' ? <HistoryPage call={call} language={language} enabled={data.management.history?.enabled} /> : tab === 'diagnostics' ? <Diagnostics data={data} call={call} refresh={refresh} /> : tab === 'settings' ? <Settings call={call} restart={openRestart} refresh={refresh} workerPid={data.snapshot.process_id} restartBlocked={data.management.draining || updatePending(data.management.update)} /> : <Updates data={data} call={call} refresh={refresh} onInstalling={() => setRestarting(true)} />}<ErrorNote error={restartError} /></main>
      <footer className="muted mt-8 flex flex-wrap items-center justify-between gap-3 text-xs"><span>AgentRouter Codex Proxy{data ? ` · ${data.snapshot.version}` : ''}</span><span>{t("История ограничена по размеру и сроку хранения.")}</span><a href="https://github.com/zerkavich/agentrouter-codex-proxy" target="_blank" rel="noreferrer" className="inline-flex min-h-10 items-center gap-1 hover:text-zinc-900 dark:hover:text-zinc-100">{t("Исходный код")} <ArrowUpRight size={13} /></a></footer>
    </div>
    <Dialog open={restartDialog} onOpenChange={setRestartDialog} title={t("Перезапустить прокси?")} description={t("По умолчанию дождёмся завершения {v0} активных запросов и временно остановим новые. Панель переподключится сама. Сохранённые настройки будут применены.", {v0: (count(data?.management.active_requests || 0))})} accept={restartForce ? t("Перезапустить сейчас") : t("Дождаться и перезапустить")} onAccept={restart} danger={restartForce}>
      <label className="mt-5 flex cursor-pointer items-start gap-3 rounded-lg border border-zinc-200 p-3 dark:border-zinc-700"><input type="checkbox" className="mt-0.5 size-4 accent-red-600" checked={restartForce} onChange={e => setRestartForce(e.target.checked)} /><span className="text-sm"><span className="block font-medium">{t("Перезапустить сразу")}</span><span className="muted mt-1 block text-xs leading-5">{t("Прервёт текущие ответы. Их потребуется повторить в Codex.")}</span></span></label>
    </Dialog>
    <Dialog open={recoverDialog} onOpenChange={setRecoverDialog} title={t("Восстановить прокси?")} description={t("Рабочий процесс будет заменён. Незавершённые запросы потребуется повторить в Codex. Панель и сохранённая история останутся доступны.")} accept={t("Восстановить прокси")} danger onAccept={async () => { try { await call('recover', { confirm: true }); setRestarting(true); await refresh(); } catch (e) { setRestartError(e); } }} />
  </div>;
}

createRoot(document.getElementById('root')).render(<App />);
