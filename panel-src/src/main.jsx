import React, { useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import * as AlertDialog from '@radix-ui/react-alert-dialog';
import { Activity, ArrowDownToLine, ArrowUpRight, Check, ChevronDown, ChevronRight, CircleCheck,
  Clock3, Code2, Gauge, Info, Link2, Moon, Pause, Play, Power, Radio, RefreshCw,
  Settings2, ShieldCheck, Sun, Terminal, Timer, Waves, WifiOff, X, Zap, PackageCheck } from 'lucide-react';
import clsx from 'clsx';
import { twMerge } from 'tailwind-merge';

const cn = (...values) => twMerge(clsx(values));
const numbers = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 });
const count = value => Number.isFinite(value) ? numbers.format(value) : '—';
const elapsed = seconds => !Number.isFinite(seconds) ? '—' : seconds >= 3600
  ? `${Math.floor(seconds / 3600)} ч ${Math.floor(seconds % 3600 / 60)} мин` : seconds >= 60
    ? `${Math.floor(seconds / 60)} мин ${Math.floor(seconds % 60)} с` : `${count(seconds)} с`;
const millis = value => !Number.isFinite(value) ? '—' : value >= 1000 ? `${count(value / 1000)} с` : `${count(value)} мс`;
const explanations = {
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
          <AlertDialog.Cancel asChild><Button>Отмена</Button></AlertDialog.Cancel>
          <AlertDialog.Action asChild><Button tone={danger ? 'danger' : 'primary'} disabled={disabled} onClick={onAccept}>{accept}</Button></AlertDialog.Action>
        </div>
      </AlertDialog.Content>
    </AlertDialog.Portal>
  </AlertDialog.Root>;
}
function ErrorNote({ error }) {
  return error ? <p role="alert" className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">{explanations[error.code] || error.message || 'Не удалось выполнить действие. Попробуйте ещё раз.'}</p> : null;
}
function Sparkline({ values, label }) {
  const max = Math.max(1, ...values), width = 280, height = 48;
  const points = values.map((value, index) => `${index * width / Math.max(1, values.length - 1)},${height - 3 - value / max * (height - 6)}`).join(' ');
  return <svg viewBox={`0 0 ${width} ${height}`} className="mt-5 h-12 w-full text-emerald-600 dark:text-emerald-400" role="img" aria-label={label}>
    <path d={`M0 ${height - 2}H${width}`} stroke="currentColor" opacity=".12" />
    {values.length > 1 && <polyline points={points} stroke="currentColor" strokeWidth="2" fill="none" strokeLinejoin="round" strokeLinecap="round" />}
  </svg>;
}
function ActionReceipt({ action }) {
  if (!action) return null;
  const label = action.state === 'rolled_back' ? 'Обновление отменено: предыдущая версия восстановлена'
    : action.state === 'verified' ? (action.action === 'update' ? 'Обновление установлено, работа прокси проверена' : 'Перезапуск завершён, работа прокси проверена')
      : action.state === 'failed' ? 'Последнее действие не завершилось' : 'Прокси перезапускается';
  return <div role="status" className={cn('mt-5 rounded-lg px-3 py-2 text-sm',
    action.state === 'verified' ? 'bg-emerald-50 text-emerald-800 dark:bg-emerald-950/30 dark:text-emerald-200'
      : 'bg-amber-50 text-amber-800 dark:bg-amber-950/30 dark:text-amber-200')}>
    <p>{label}{Number.isFinite(action.duration_s) ? ' · ' + elapsed(action.duration_s) : ''}</p>
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
  const healthLabel = manage.paused ? 'Новые запросы на паузе' : manage.draining ? 'Ожидание перезапуска' : snapshot.status === 'rate_limited' ? 'Ожидание квоты' : snapshot.status === 'busy' ? 'Очередь запросов' : 'Прокси готов';
  return <div className="space-y-5">
    <section className="panel p-5 sm:p-7">
      <div className="flex flex-wrap items-start justify-between gap-5">
        <div className="flex gap-4"><div className="flex size-12 shrink-0 items-center justify-center rounded-xl bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-400"><ShieldCheck size={26} strokeWidth={1.75} /></div>
          <div><div className="flex flex-wrap items-center gap-2"><h2 className="text-xl font-semibold">{healthLabel}</h2><span className="tag bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400">v{snapshot.version}</span></div>
            <p className="muted mt-1.5 text-sm">Один локальный процесс для всех пользователей этого ПК.</p>
            <div className="muted mt-3 flex flex-wrap items-center gap-x-5 gap-y-2 text-xs"><span className="inline-flex items-center gap-1.5"><Clock3 size={14} /> Работает {elapsed(snapshot.uptime_seconds)}</span><span className="mono">{window.location.host}</span></div>
          </div>
        </div>
        <div className="flex flex-wrap gap-2"><Button icon={manage.paused ? Play : Pause} disabled={working || manage.draining} onClick={() => manage.paused ? pause(false) : setPauseDialog(true)}>{manage.paused ? 'Продолжить' : 'Пауза'}</Button>
          <Button icon={RefreshCw} onClick={restart} disabled={!manage.restart_supported || manage.draining}>Перезапустить</Button></div>
      </div><ErrorNote error={error} />
      {manage.draining && <div className="mt-5 flex flex-wrap items-center justify-between gap-3 rounded-xl bg-amber-50 p-4 text-sm text-amber-800 dark:bg-amber-950/25 dark:text-amber-200"><span>Ждём завершения {count(manage.active_requests)} запросов. Новые временно приостановлены.</span><Button onClick={async () => { try { await call('restart/cancel', {}); await refresh(); } catch (e) { setError(e); } }}>Отменить перезапуск</Button></div>}
      {manage.pending_settings && <p className="mt-5 rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:bg-amber-950/30 dark:text-amber-200">Настройки сохранены. Перезапустите прокси, чтобы применить их.</p>}
      <ActionReceipt action={manage.last_action} />
    </section>
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
      {[{ title: 'Активные запросы', value: manage.active_requests, suffix: '', icon: Activity, note: 'Включая ответы, которые ещё стримятся', plot: history.map(x => x.active) },
        { title: 'Соединения', value: pool.in_use, suffix: ` / ${pool.max_inflight}`, icon: Link2, note: `${count(pool.waiters)} в очереди · ${count(pool.idle_connections)} готовы к повтору`, plot: history.map(x => x.inUse) },
        { title: 'После 429', value: rate.queued_requests, suffix: '', icon: Timer, note: rate.cooling_partitions ? `Пауза до ${elapsed(rate.longest_cooldown_remaining_s)}` : 'Сейчас нет задерживающей квоты', plot: history.map(x => x.queued) },
        { title: 'Запросов с запуска', value: stats.responses_requests, suffix: '', icon: Radio, note: `${count(stats.successful_recoveries)} восстановлены автоматически`, plot: history.map(x => x.newRequests) }].map(item => <section key={item.title} className="panel p-5"><div className="muted flex items-center justify-between text-xs font-medium"><span>{item.title}</span><item.icon size={16} /></div><p className="data mt-4 text-3xl font-semibold">{count(item.value)}<span className="muted text-lg font-normal">{item.suffix}</span></p><p className="muted mt-2 min-h-8 text-xs leading-4">{item.note}</p><Sparkline values={item.plot} label={`Последние значения: ${item.title}`} /></section>)}
    </div>
    <div className="grid gap-5 lg:grid-cols-3">
      <section className="panel p-5 sm:p-6 lg:col-span-2"><div className="flex flex-wrap items-center justify-between gap-3"><h2 className="font-semibold">Что происходит сейчас</h2><span className="muted text-xs">Обновляется каждые 2 секунды</span></div>
        {snapshot.findings.length ? <ul className="mt-5 divide-y divide-zinc-100 dark:divide-zinc-800">{snapshot.findings.map(item => <li key={item.code} className="flex items-start gap-3 py-3"><Info className="mt-0.5 shrink-0 text-amber-500" size={17} /><div><p className="text-sm leading-6">{findText[item.code] || item.message}</p><span className="muted text-xs">{item.kind === 'current' ? 'Сейчас' : 'С момента запуска'}</span></div></li>)}</ul> : <div className="mt-6 flex items-center gap-4 rounded-xl bg-zinc-50 p-5 dark:bg-zinc-950/70"><CircleCheck className="text-emerald-500" size={24} /><div><p className="text-sm font-medium">Очереди свободны</p><p className="muted mt-1 text-sm">Признаков текущей блокировки нет. Провайдер может продолжать генерацию.</p></div></div>}
        <div className="mt-5 grid gap-4 border-t border-zinc-100 pt-5 dark:border-zinc-800 sm:grid-cols-3"><div><p className="muted text-xs">Таймауты пула</p><p className="data mt-2 font-semibold">{count(stats.pool_timeouts)}</p></div><div><p className="muted text-xs">Встречено 429</p><p className="data mt-2 font-semibold">{count(stats.rate_limits_caught)}</p></div><div><p className="muted text-xs">Время ожидания квоты</p><p className="data mt-2 font-semibold">{elapsed(rate.wait_seconds_total)}</p><p className="muted mt-1 text-xs">Сумма по всем запросам</p></div></div>
      </section>
      <section className="panel p-5 sm:p-6"><div className="flex items-center justify-between"><h2 className="font-semibold">Передача данных</h2><Zap className="muted" size={17} /></div><div className="mt-6 space-y-5"><div className="flex justify-between text-sm"><span className="muted">Сжатие gzip</span><span className={compression.enabled ? 'text-emerald-600 dark:text-emerald-400' : ''}>{compression.enabled ? 'Включено' : 'Выключено'}</span></div><div className="flex justify-between text-sm"><span className="muted">Движок</span><span className="mono">{compression.backend === 'isal' ? 'ISA-L' : 'stdlib'}</span></div><div className="flex justify-between text-sm"><span className="muted">Задач сжатия</span><span className="data">{count(compression.jobs_inflight)} / {count(compression.max_jobs)}</span></div><div className="flex justify-between text-sm"><span className="muted">Очистка соединений</span><span>{pool.cleanup_active ? 'Работает' : 'Не активна'}</span></div></div><p className="muted mt-6 border-t border-zinc-100 pt-4 text-xs leading-5 dark:border-zinc-800">Диагностика и панель не отправляют запросы модели. Ключи остаются в Codex.</p></section>
    </div>
    <Dialog open={pauseDialog} onOpenChange={setPauseDialog} title="Приостановить новые запросы?" description="Текущие ответы продолжатся. Новые запросы получат временную ошибку до нажатия «Продолжить». Пауза действует до перезапуска прокси." accept="Поставить на паузу" onAccept={() => pause(true)} />
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
    <section className="panel p-5 sm:p-7"><div className="flex flex-wrap items-start justify-between gap-6"><div><h2 className="text-xl font-semibold">Профиль задержек</h2><p className="muted mt-2 max-w-xl text-sm leading-6">Включите измерение, затем отправьте запрос в Codex. Будет видно, где проходит время: подготовка, соединение, квота или ожидание провайдера.</p></div><span className={cn('tag', active ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-400' : 'bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400')}><span className={cn('size-1.5 rounded-full', active ? 'bg-emerald-500' : 'bg-zinc-400')} />{active ? snapshot.timing_mode === 'always' ? 'Постоянный профиль' : `Идёт запись · ${elapsed(snapshot.profile_remaining_s)}` : 'Измерение выключено'}</span></div>
      <div className="mt-6 flex flex-wrap items-center gap-3"><label htmlFor="profile-duration" className="label">Длительность</label><select id="profile-duration" className="field w-auto" value={duration} onChange={e => setDuration(e.target.value)}>{[30, 60, 120, 300].map(v => <option key={v} value={v}>{v < 60 ? `${v} секунд` : `${v / 60} мин`}</option>)}</select><Button tone="primary" icon={Play} disabled={working} onClick={() => profile()}>{active ? 'Продлить запись' : 'Начать измерение'}</Button>{active && snapshot.timing_mode !== 'always' && <Button icon={Pause} disabled={working} onClick={() => profile(true)}>Остановить</Button>}<a className="button button-default sm:ml-auto" href="/_control/api/report" download><ArrowDownToLine size={16} />Скачать отчёт</a></div><ErrorNote error={error} />
      <p className="muted mt-4 text-xs leading-5">Выключится автоматически. Накопленные результаты останутся до перезапуска. Переписка и ключи не записываются.</p>
    </section>
    <section className="panel overflow-hidden"><div className="p-5 sm:p-6"><h2 className="font-semibold">Этапы запроса</h2><p className="muted mt-1 text-xs">Интервалы могут пересекаться. Их нельзя складывать или считать временем CPU прокси.</p></div>
      {!measured ? <div className="flex flex-col items-center px-5 py-14 text-center"><div className="mb-4 rounded-2xl bg-zinc-100 p-4 dark:bg-zinc-800"><Gauge size={28} className="muted" /></div><h3 className="font-medium">Пока нет измерений</h3><p className="muted mt-2 max-w-md text-sm leading-6">Запустите профиль и воспроизведите задержку. Обычный режим работает без подробных таймеров.</p><Button className="mt-5" tone="primary" disabled={working} onClick={() => profile()} icon={Play}>Начать измерение</Button></div> : <div className="overflow-x-auto"><table className="w-full min-w-[640px] text-left text-sm"><thead className="bg-zinc-50 text-xs text-zinc-500 dark:bg-zinc-950/50 dark:text-zinc-400"><tr><th className="px-5 py-3 font-medium sm:px-6">Этап</th><th className="px-3 py-3 font-medium">Среднее</th><th className="px-3 py-3 font-medium">p95 ≤</th><th className="px-3 py-3 font-medium">Замеров</th><th className="px-5 py-3 font-medium">Сейчас</th></tr></thead><tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">{Object.entries(stages).map(([key, [label, explanation]]) => { const timing = snapshot.timings[key]; return <tr key={key}><td className="w-2/5 px-5 py-3 sm:px-6"><span className="font-medium">{label}</span><p className="muted mt-0.5 text-xs">{explanation}</p></td><td className="data px-3 py-3"><span>{millis(timing.mean_ms)}</span><div className="mt-1.5 h-1 w-24 overflow-hidden rounded-full bg-zinc-100 dark:bg-zinc-800"><div className="h-full bg-emerald-500/70" style={{ width: `${Math.max(0, (timing.mean_ms || 0) / max * 100)}%` }} /></div></td><td className="data px-3 py-3">{timing.count && timing.p95_bucket_upper_ms == null ? '> 600 с' : millis(timing.p95_bucket_upper_ms)}</td><td className="data px-3 py-3">{count(timing.count)}</td><td className="data px-5 py-3">{timing.active ? <span className="tag bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-400">{count(timing.active)} в работе</span> : <span className="muted">—</span>}</td></tr>; })}</tbody></table></div>}
      <p className="muted border-t border-zinc-100 px-5 py-4 text-xs leading-5 dark:border-zinc-800 sm:px-6">p95 — верхняя граница корзины гистограммы, а не точный процентиль. Замеры включают ошибки и отмены, а сетевые этапы — ожидание провайдера.</p>
    </section>
  </div>;
}

function Settings({ call, restart, refresh, workerPid }) {
  const [source, setSource] = useState(null), [draft, setDraft] = useState({}), [error, setError] = useState(null), [saved, setSaved] = useState(false), [working, setWorking] = useState(false);
  const load = useCallback(async () => { try { const value = await call(null, null, 'settings'); setSource(value); setDraft(value.saved); setError(null); setSaved(false); } catch (e) { setError(e); } }, [call]);
  useEffect(() => { load(); }, [load, workerPid]);
  const changes = source ? Object.fromEntries(Object.entries(draft).filter(([key, value]) => value !== source.saved[key])) : {};
  const dirty = Object.keys(changes).length > 0;
  async function save() {
    setWorking(true); setError(null);
    try { const next = await call('settings', { changes, revision: source.revision }); setSource({ ...source, saved: next.values, revision: next.revision, restart_required: next.restart_required }); setDraft(next.values); setSaved(true); await refresh(); } catch (e) { setError(e); } finally { setWorking(false); }
  }
  return <div className="space-y-5"><section className="panel p-5 sm:p-6"><div className="flex flex-wrap items-start justify-between gap-5"><div><h2 className="text-xl font-semibold">Настройки прокси</h2><p className="muted mt-2 text-sm leading-6">Сохраняются для всех пользователей этого ПК. Применяются после перезапуска; настройки Codex и ключи остаются на месте.</p></div><Button icon={RefreshCw} onClick={load}>Обновить значения</Button></div><ErrorNote error={error} /></section>
    {!source ? <div className="panel p-6"><p className="muted">Загружаем настройки…</p></div> : <>
      {Object.entries({ compression: ['Передача и сжатие', Zap], recovery: ['Восстановление после ошибок', ShieldCheck], network: ['Подключение', Link2], streaming: ['Стриминг', Waves], diagnostics: ['Диагностика', Gauge] }).map(([group, [title, Icon]]) => <section key={group} className="panel p-5 sm:p-6"><h3 className="flex items-center gap-2.5 font-semibold"><Icon size={18} className="muted" />{title}</h3><div className="mt-6 grid gap-x-8 gap-y-6 md:grid-cols-2">{Object.entries(source.schema).filter(([, spec]) => spec.group === group).map(([name, spec]) => { const [label, help] = settingsCopy[name] || [name, '']; const invalid = error?.fields?.includes(name); return <div key={name} className={spec.kind === 'bool' ? 'md:col-span-2' : ''}>{spec.kind === 'bool' ? <label className="flex cursor-pointer items-center justify-between gap-4 rounded-xl bg-zinc-50 p-4 dark:bg-zinc-950/60"><div><span className="label">{label}</span><p className="muted mt-1 text-xs leading-5">{help}</p></div><input aria-label={label} type="checkbox" className="size-5 shrink-0 accent-emerald-600" checked={Boolean(draft[name])} onChange={e => { setDraft({ ...draft, [name]: e.target.checked }); setSaved(false); }} /></label> : <><label className="label" htmlFor={name}>{label}</label>{spec.kind === 'enum' ? <select id={name} className="field mt-2" value={draft[name]} onChange={e => { setDraft({ ...draft, [name]: e.target.value }); setSaved(false); }}>{spec.values.map(value => <option key={value} value={value}>{value === 'credential' ? 'По ключу' : 'Общая квота'}</option>)}</select> : <input id={name} type="number" min={spec.min} max={spec.max} step={spec.kind === 'int' ? 1 : 'any'} value={draft[name] ?? ''} aria-invalid={invalid || undefined} className={cn('field data mt-2', invalid && 'border-red-500')} onChange={e => { setDraft({ ...draft, [name]: e.target.value === '' ? '' : Number(e.target.value) }); setSaved(false); }} />}<p className="muted mt-1.5 text-xs leading-5">{help}</p></>}</div>; })}</div></section>)}
      <section className="panel p-5 sm:p-6"><div className="flex flex-wrap items-center justify-between gap-4"><div><p className="text-sm font-medium">{dirty ? `Изменено настроек: ${Object.keys(changes).length}` : saved ? 'Настройки сохранены' : 'Изменений нет'}</p><p className="muted mt-1 text-xs">{source.restart_required || dirty ? 'Перезапуск применит сохранённые значения.' : 'Текущая версия уже использует эти значения.'}</p></div><div className="flex flex-wrap gap-2"><Button disabled={!dirty || working} onClick={() => { setDraft(source.saved); setSaved(false); }}>Сбросить изменения</Button><Button tone="primary" icon={Check} disabled={!dirty || working} onClick={save}>{working ? 'Сохраняем…' : 'Сохранить'}</Button>{source.restart_required && !dirty && <Button icon={RefreshCw} onClick={restart}>Применить перезапуском</Button>}</div></div><ErrorNote error={error} /></section>
    </>}
  </div>;
}

function Updates({ data, call, refresh, onInstalling }) {
  const [working, setWorking] = useState(null), [error, setError] = useState(null), [installDialog, setInstallDialog] = useState(false);
  const update = data.management.update;
  async function check() { setWorking('check'); setError(null); try { const result = await call('updates/check', {}); if (result.state === 'failed') throw { code: result.error_code }; await refresh(); } catch (e) { setError(e); } finally { setWorking(null); } }
  async function prepare() { setWorking('prepare'); setError(null); try { const result = await call('updates/prepare', { commit: update.commit }); if (result.state === 'failed') throw { code: result.error_code }; await refresh(); } catch (e) { setError(e); } finally { setWorking(null); } }
  async function install() { setWorking('install'); setError(null); try { await call('updates/install', { commit: update.commit }); onInstalling(); await refresh(); } catch (e) { setError(e); } finally { setWorking(null); } }
  return <div className="space-y-5"><section className="panel p-5 sm:p-7"><div className="flex flex-wrap items-start justify-between gap-5"><div className="flex gap-4"><div className="flex size-12 shrink-0 items-center justify-center rounded-xl bg-zinc-100 dark:bg-zinc-800"><PackageCheck size={25} className="text-emerald-600 dark:text-emerald-400" /></div><div><h2 className="text-xl font-semibold">Обновления</h2><p className="muted mt-2 text-sm">Установлена версия <span className="data font-medium text-zinc-800 dark:text-zinc-200">{data.snapshot.version}</span></p><p className="muted mt-1 text-xs">zerkavich / agentrouter-codex-proxy · main</p></div></div><Button icon={RefreshCw} tone="primary" onClick={check} disabled={working}>{working === 'prepare' ? 'Подготавливаем пакет…' : working === 'install' ? 'Перезапускаем…' : working ? 'Проверяем GitHub…' : 'Проверить обновления'}</Button></div><ErrorNote error={error} />
      {['checked', 'prepared'].includes(update.state) && <div className="mt-6 rounded-xl bg-zinc-50 p-5 dark:bg-zinc-950/70"><h3 className="font-medium">{update.state === 'prepared' ? `Пакет ${update.latest_version} проверен и готов` : update.update_available ? `Доступно обновление${update.latest_version ? ` ${update.latest_version}` : ''}` : 'Установлена актуальная версия'}</h3><p className="muted mt-2 text-sm leading-6">{update.title}</p><div className="mt-4 flex flex-wrap gap-3">{update.install_supported && update.update_available && <Button tone="primary" disabled={working || data.management.draining} onClick={update.state === 'prepared' ? () => setInstallDialog(true) : prepare} icon={PackageCheck}>{working ? 'Подготавливаем…' : update.state === 'prepared' ? 'Установить обновление' : 'Подготовить обновление'}</Button>}<a className="button button-default" href={update.url} target="_blank" rel="noreferrer">Изменения <ArrowUpRight size={15} /></a><a className="button button-default" href="https://github.com/zerkavich/agentrouter-codex-proxy/blob/main/docs/windows-installation.md" target="_blank" rel="noreferrer">Инструкция установки <ArrowUpRight size={15} /></a></div>{update.install_supported && <p className="muted mt-4 text-xs leading-5">Подготовка проверяет четыре CI-задачи и версии зависимостей. При установке сохраняется резервная копия; ошибка запуска вызывает откат.</p>}</div>}
      {update.state === 'not_checked' && <p className="muted mt-6 rounded-xl bg-zinc-50 p-5 text-sm leading-6 dark:bg-zinc-950/70">Проверка выполняется по нажатию. Панель не обращается к GitHub в фоне.</p>}
    </section><section className="panel p-5 sm:p-6"><h2 className="font-semibold">Изменения этой версии</h2><ul className="muted mt-4 space-y-3 text-sm leading-6"><li className="flex gap-3"><Check size={17} className="mt-1 shrink-0 text-emerald-500" />Управление, состояние и профиль прямо в браузере.</li><li className="flex gap-3"><Check size={17} className="mt-1 shrink-0 text-emerald-500" />Пауза новых запросов и перезапуск после завершения текущих.</li><li className="flex gap-3"><Check size={17} className="mt-1 shrink-0 text-emerald-500" />Настройки для всех пользователей отдельно от ключей и конфигурации Codex.</li><li className="flex gap-3"><Check size={17} className="mt-1 shrink-0 text-emerald-500" />Экспорт отчёта без переписки и учётных данных.</li></ul></section><Dialog open={installDialog} onOpenChange={setInstallDialog} title={`Установить ${update.latest_version || 'обновление'}?`} description="Дождёмся завершения текущих запросов, сохраним предыдущую версию и перезапустим прокси с новым пакетом. Панель переподключится. Ключи и история Codex сохраняются." accept="Установить и перезапустить" onAccept={install} /></div>;
}

function App() {
  const [tab, setTab] = useState(() => ['diagnostics', 'settings', 'updates'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'overview');
  const [theme, setTheme] = useState(() => { try { return localStorage.getItem('proxy-panel-theme') || 'dark'; } catch { return 'dark'; } });
  const [data, setData] = useState(null), [history, setHistory] = useState([]), [connected, setConnected] = useState(true), [initialError, setInitialError] = useState(null);
  const [restartDialog, setRestartDialog] = useState(false), [restartForce, setRestartForce] = useState(false), [restartError, setRestartError] = useState(null), [restarting, setRestarting] = useState(false);
  const [lastSeen, setLastSeen] = useState(null), [refreshing, setRefreshing] = useState(false);
  const token = useRef(null), sessionPromise = useRef(null), previous = useRef(null), statusFlight = useRef(null), disposed = useRef(false);
  useEffect(() => { document.documentElement.classList.toggle('dark', theme === 'dark'); try { localStorage.setItem('proxy-panel-theme', theme); } catch {} }, [theme]);
  const session = useCallback(async () => {
    if (token.current) return token.current;
    if (!sessionPromise.current) sessionPromise.current = fetch('/_control/api/session', { cache: 'no-store', signal: AbortSignal.timeout(4000) }).then(async response => {
      const result = await response.json(); if (!response.ok) throw { code: result.error_code }; token.current = result.token; return result.token;
    }).finally(() => { sessionPromise.current = null; });
    return sessionPromise.current;
  }, []);
  const call = useCallback(async (action, value, read = null) => {
    const controlToken = await session();
    const response = await fetch('/_control/api/' + (read || action), {
      method: read ? 'GET' : 'POST', cache: 'no-store',
      headers: read ? {} : { 'Content-Type': 'application/json', 'X-Proxy-Control': controlToken },
      body: read ? undefined : JSON.stringify(value), signal: AbortSignal.timeout(action?.startsWith('updates/') ? 90000 : 7000),
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
        const requests = result.snapshot.counts.responses_requests;
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
  useEffect(() => { const handler = () => setTab(['diagnostics', 'settings', 'updates'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'overview'); addEventListener('hashchange', handler); return () => removeEventListener('hashchange', handler); }, []);
  async function restart() {
    setRestartError(null);
    try { await call('restart', { force: restartForce }); setRestarting(true); await refresh(); } catch (error) { setRestartError(error); }
  }
  const tabs = [{ id: 'overview', label: 'Обзор', icon: Activity }, { id: 'diagnostics', label: 'Диагностика', icon: Gauge }, { id: 'settings', label: 'Настройки', icon: Settings2 }, { id: 'updates', label: 'Обновления', icon: PackageCheck }];
  const openRestart = () => { setRestartForce(false); setRestartDialog(true); };
  return <div className="min-h-dvh"><a href="#main" className="sr-only z-50 rounded-lg bg-emerald-600 p-3 text-white focus:not-sr-only focus:fixed focus:left-4 focus:top-4">Перейти к содержимому</a>
    <header className="border-b border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-950"><div className="mx-auto flex max-w-7xl items-center justify-between gap-3 px-4 py-4 sm:px-8"><a href="#overview" className="flex items-center gap-3"><span className="flex size-10 items-center justify-center rounded-xl bg-zinc-900 text-emerald-400 dark:bg-zinc-800"><Code2 size={23} strokeWidth={1.75} /></span><span><span className="block text-sm font-semibold sm:text-base">AgentRouter</span><span className="muted block text-xs">Codex Proxy</span></span></a><div className="flex items-center gap-3"><span className="hidden items-center gap-1.5 text-xs text-zinc-500 sm:inline-flex dark:text-zinc-400"><ShieldCheck size={14} />Локальная панель</span><Button icon={theme === 'dark' ? Sun : Moon} aria-label={theme === 'dark' ? 'Светлая тема' : 'Тёмная тема'} className="size-10 p-0" onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')} /></div></div></header>
    <div className="mx-auto max-w-7xl px-4 pb-12 pt-7 sm:px-8 sm:pt-10"><div className="mb-7 flex flex-wrap items-start justify-between gap-4"><div><p className="muted mb-2 text-xs font-medium">УПРАВЛЕНИЕ ПРОКСИ</p><h1 className="text-2xl font-semibold sm:text-3xl">{tabs.find(item => item.id === tab)?.label}</h1></div><div className="flex items-center gap-3 pt-1"><span className={cn('tag', connected && data ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-400' : 'bg-amber-50 text-amber-700 dark:bg-amber-950/50 dark:text-amber-300')}><span className={cn('size-1.5 rounded-full', connected && data ? 'bg-emerald-500' : 'bg-amber-500')} />{connected && data ? 'Подключено' : restarting ? 'Перезапуск' : 'Подключение'}</span><Button icon={RefreshCw} aria-label="Обновить состояние" className="size-10 p-0" disabled={refreshing} onClick={async () => { setRefreshing(true); await refresh(); setRefreshing(false); }} /></div></div>
      <nav aria-label="Разделы управления" className="mb-7 flex gap-1 overflow-x-auto border-b border-zinc-200 dark:border-zinc-800">{tabs.map(({ id, label, icon: Icon }) => <a key={id} href={`#${id}`} aria-current={tab === id ? 'page' : undefined} className={cn('mb-[-1px] inline-flex min-h-11 shrink-0 items-center gap-2 border-b-2 px-3 py-3 text-sm font-medium sm:px-4', tab === id ? 'border-emerald-500 text-emerald-700 dark:text-emerald-400' : 'border-transparent text-zinc-500 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100')}><Icon size={17} strokeWidth={1.75} />{label}</a>)}</nav>
      {!connected && data && <div role="status" className="mb-5 flex items-start gap-3 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200"><WifiOff size={18} className="mt-0.5 shrink-0" /><div><p className="font-medium">{restarting ? 'Прокси перезапускается' : 'Связь с прокси прервалась'}</p><p className="mt-1 leading-6">Панель переподключится автоматически. Последние значения {lastSeen ? `получены в ${lastSeen.toLocaleTimeString('ru-RU')}` : 'сохранены'}.</p></div></div>}
      <main id="main" tabIndex={-1}>{!data ? <section className="panel p-6 sm:p-10"><h2 className="font-semibold">{connected ? 'Подключаемся к прокси…' : 'Прокси пока недоступен'}</h2><p className="muted mt-3 text-sm leading-6">{connected ? 'Загружаем локальное состояние.' : 'Проверьте, что сервис запущен. Эта страница попробует подключиться снова.'}</p>{!connected && <ErrorNote error={initialError} />}<div className="mt-6 grid gap-4 sm:grid-cols-3">{[1, 2, 3].map(i => <div key={i} className="h-24 rounded-xl bg-zinc-100 dark:bg-zinc-800" />)}</div></section> : tab === 'overview' ? <Overview data={data} history={history} call={call} refresh={refresh} restart={openRestart} /> : tab === 'diagnostics' ? <Diagnostics data={data} call={call} refresh={refresh} /> : tab === 'settings' ? <Settings call={call} restart={openRestart} refresh={refresh} workerPid={data.snapshot.process_id} /> : <Updates data={data} call={call} refresh={refresh} onInstalling={() => setRestarting(true)} />}<ErrorNote error={restartError} /></main>
      <footer className="muted mt-8 flex flex-wrap items-center justify-between gap-3 text-xs"><span>AgentRouter Codex Proxy{data ? ` · ${data.snapshot.version}` : ''}</span><span>История графиков хранится только в этой вкладке.</span><a href="https://github.com/zerkavich/agentrouter-codex-proxy" target="_blank" rel="noreferrer" className="inline-flex min-h-10 items-center gap-1 hover:text-zinc-900 dark:hover:text-zinc-100">Исходный код <ArrowUpRight size={13} /></a></footer>
    </div>
    <Dialog open={restartDialog} onOpenChange={setRestartDialog} title="Перезапустить прокси?" description={`По умолчанию дождёмся завершения ${count(data?.management.active_requests || 0)} активных запросов и временно остановим новые. Панель переподключится сама. Сохранённые настройки будут применены.`} accept={restartForce ? 'Перезапустить сейчас' : 'Дождаться и перезапустить'} onAccept={restart} danger={restartForce}>
      <label className="mt-5 flex cursor-pointer items-start gap-3 rounded-lg border border-zinc-200 p-3 dark:border-zinc-700"><input type="checkbox" className="mt-0.5 size-4 accent-red-600" checked={restartForce} onChange={e => setRestartForce(e.target.checked)} /><span className="text-sm"><span className="block font-medium">Перезапустить сразу</span><span className="muted mt-1 block text-xs leading-5">Прервёт текущие ответы. Их потребуется повторить в Codex.</span></span></label>
    </Dialog>
  </div>;
}

createRoot(document.getElementById('root')).render(<App />);
