import {
  Activity,
  ArrowDownRight,
  ArrowUpRight,
  Bell,
  BookOpenText,
  Check,
  ChevronRight,
  CircleGauge,
  Database,
  ExternalLink,
  FileText,
  LayoutDashboard,
  Menu,
  Search,
  Settings2,
  ShieldCheck,
  Sparkles,
  Target,
  X,
} from "lucide-react";
import { useMemo, useState } from "react";

const signals = [
  {
    ticker: "SBER",
    company: "Сбербанк",
    direction: "up",
    directionLabel: "Вверх",
    action: "Рассмотреть покупку",
    score: "+42.7",
    confidence: 76,
    strength: "Сильный",
    horizon: "3 торговых дня",
    updated: "12 мин назад",
    price: "317,42 ₽",
    change: "+1,8%",
    summary:
      "Свежая отчётность оказалась сильнее ожиданий, а текущая реакция рынка пока не выглядит полной.",
    factors: [
      { label: "Новости и события", value: 38, tone: "positive", detail: "+0.38" },
      { label: "Реакция рынка", value: 21, tone: "positive", detail: "+0.21" },
      { label: "Показатели компании", value: 14, tone: "positive", detail: "+0.14" },
      { label: "Риск и волатильность", value: 8, tone: "negative", detail: "−0.08" },
    ],
    evidence: [
      {
        source: "Интерфакс",
        time: "10:18",
        title: "Чистая прибыль за 7 месяцев выросла быстрее консенсуса",
        tag: "Новый факт",
      },
      {
        source: "Отчётность эмитента",
        time: "09:52",
        title: "Рентабельность капитала удержалась выше целевого уровня",
        tag: "Подтверждение",
      },
      {
        source: "MOEX",
        time: "10:34",
        title: "Объём выше среднего, ценовая реакция остаётся умеренной",
        tag: "Рынок",
      },
    ],
    invalidation: "Результаты будут пересмотрены при ухудшении маржи или росте риска рынка.",
  },
  {
    ticker: "LKOH",
    company: "Лукойл",
    direction: "up",
    directionLabel: "Вверх",
    action: "Добавить в наблюдение",
    score: "+29.4",
    confidence: 69,
    strength: "Умеренный",
    horizon: "5 торговых дней",
    updated: "28 мин назад",
    price: "6 714 ₽",
    change: "+0,7%",
    summary:
      "Денежный поток и дивидендные ожидания поддерживают идею, но часть эффекта уже отражена в цене.",
    factors: [
      { label: "Новости и события", value: 24, tone: "positive", detail: "+0.24" },
      { label: "Показатели компании", value: 20, tone: "positive", detail: "+0.20" },
      { label: "Реакция рынка", value: 9, tone: "positive", detail: "+0.09" },
      { label: "Уже учтено в цене", value: 13, tone: "negative", detail: "−0.13" },
    ],
    evidence: [
      {
        source: "Отчётность эмитента",
        time: "09:40",
        title: "Свободный денежный поток превысил прошлогодний уровень",
        tag: "Новый факт",
      },
      {
        source: "MOEX",
        time: "10:10",
        title: "Бумага уже прибавляла в предыдущие две сессии",
        tag: "Рынок",
      },
    ],
    invalidation: "Сигнал станет нейтральным при снижении цены нефти ниже контрольного уровня.",
  },
  {
    ticker: "YDEX",
    company: "Яндекс",
    direction: "neutral",
    directionLabel: "Нейтрально",
    action: "Подождать новых данных",
    score: "+7.1",
    confidence: 58,
    strength: "Слабый",
    horizon: "3 торговых дня",
    updated: "41 мин назад",
    price: "4 168 ₽",
    change: "+0,2%",
    summary:
      "Позитивные операционные новости компенсируются высокой оценкой и отсутствием подтверждения объёмом.",
    factors: [
      { label: "Новости и события", value: 19, tone: "positive", detail: "+0.19" },
      { label: "Рост бизнеса", value: 15, tone: "positive", detail: "+0.15" },
      { label: "Оценка компании", value: 18, tone: "negative", detail: "−0.18" },
      { label: "Подтверждение рынком", value: 9, tone: "negative", detail: "−0.09" },
    ],
    evidence: [
      {
        source: "РБК",
        time: "09:14",
        title: "Компания анонсировала расширение облачного направления",
        tag: "Событие",
      },
      {
        source: "MOEX",
        time: "10:02",
        title: "Объём торгов остаётся ниже двадцатидневного среднего",
        tag: "Рынок",
      },
    ],
    invalidation: "Потребуется новый сильный факт или подтверждение движением цены и объёма.",
  },
  {
    ticker: "NVTK",
    company: "Новатэк",
    direction: "down",
    directionLabel: "Вниз",
    action: "Пересмотреть позицию",
    score: "−36.8",
    confidence: 73,
    strength: "Сильный",
    horizon: "5 торговых дней",
    updated: "1 ч назад",
    price: "1 081 ₽",
    change: "−2,1%",
    summary:
      "Новые ограничения повышают неопределённость по срокам проектов, а рынок подтверждает ухудшение ожиданий.",
    factors: [
      { label: "Новости и события", value: 35, tone: "negative", detail: "−0.35" },
      { label: "Реакция рынка", value: 22, tone: "negative", detail: "−0.22" },
      { label: "Риск исполнения", value: 18, tone: "negative", detail: "−0.18" },
      { label: "Финансовая устойчивость", value: 12, tone: "positive", detail: "+0.12" },
    ],
    evidence: [
      {
        source: "Reuters",
        time: "08:57",
        title: "Обновлены ограничения для поставок технологического оборудования",
        tag: "Высокий эффект",
      },
      {
        source: "MOEX",
        time: "09:27",
        title: "Снижение подтверждено объёмом выше среднего",
        tag: "Рынок",
      },
    ],
    invalidation: "Официальное подтверждение неизменных сроков проекта отменит текущую гипотезу.",
  },
  {
    ticker: "MGNT",
    company: "Магнит",
    direction: "neutral",
    directionLabel: "Нейтрально",
    action: "Ничего не делать",
    score: "−4.3",
    confidence: 52,
    strength: "Слабый",
    horizon: "3 торговых дня",
    updated: "2 ч назад",
    price: "4 706 ₽",
    change: "−0,3%",
    summary:
      "Информационный фон противоречив, а свежих данных недостаточно для решения с преимуществом.",
    factors: [
      { label: "Новости и события", value: 8, tone: "negative", detail: "−0.08" },
      { label: "Показатели компании", value: 12, tone: "positive", detail: "+0.12" },
      { label: "Реакция рынка", value: 5, tone: "negative", detail: "−0.05" },
      { label: "Качество данных", value: 7, tone: "negative", detail: "−0.07" },
    ],
    evidence: [
      {
        source: "ТАСС",
        time: "08:20",
        title: "Компания обновила планы открытия магазинов",
        tag: "Повтор",
      },
      {
        source: "MOEX",
        time: "09:31",
        title: "Выраженной реакции цены и объёма нет",
        tag: "Рынок",
      },
    ],
    invalidation: "Сигнал изменится после публикации операционных результатов.",
  },
];

const directionMeta = {
  up: { icon: ArrowUpRight, label: "Вверх", actionLead: "Что можно сделать" },
  neutral: { icon: Activity, label: "Нейтрально", actionLead: "Рекомендуемое действие" },
  down: { icon: ArrowDownRight, label: "Вниз", actionLead: "Что стоит проверить" },
};

const filters = [
  { value: "all", label: "Все" },
  { value: "up", label: "Вверх" },
  { value: "neutral", label: "Нейтрально" },
  { value: "down", label: "Вниз" },
];

function BrandMark() {
  return (
    <div className="brand-mark" aria-hidden="true">
      <span />
      <span />
      <span />
    </div>
  );
}

function DirectionBadge({ direction, compact = false }) {
  const meta = directionMeta[direction];
  const Icon = meta.icon;
  return (
    <span className={`direction-badge direction-badge--${direction} ${compact ? "is-compact" : ""}`}>
      <Icon size={compact ? 13 : 15} strokeWidth={2.4} />
      {meta.label}
    </span>
  );
}

function SignalRow({ signal, active, onSelect }) {
  return (
    <button
      type="button"
      className={`signal-row ${active ? "is-active" : ""}`}
      onClick={onSelect}
      aria-pressed={active}
    >
      <span className="signal-row__company">
        <span className={`ticker-mark ticker-mark--${signal.direction}`}>{signal.ticker.slice(0, 2)}</span>
        <span>
          <strong>{signal.ticker}</strong>
          <small>{signal.company}</small>
        </span>
      </span>
      <DirectionBadge direction={signal.direction} compact />
      <span className={`signal-row__score signal-row__score--${signal.direction}`}>{signal.score}</span>
      <span className="signal-row__confidence">
        <span className="confidence-track" aria-hidden="true">
          <span style={{ width: `${signal.confidence}%` }} />
        </span>
        <small>{signal.confidence}%</small>
      </span>
      <span className="signal-row__horizon">{signal.horizon.replace(" торговых дней", " дн.")}</span>
      <span className="signal-row__price">
        <strong>{signal.price}</strong>
        <small className={`market-change market-change--${signal.direction}`}>{signal.change}</small>
      </span>
      <ChevronRight className="signal-row__chevron" size={16} />
    </button>
  );
}

function FactorBar({ factor }) {
  return (
    <div className="factor-row">
      <div className="factor-row__label">
        <span>{factor.label}</span>
        <strong className={`factor-value factor-value--${factor.tone}`}>{factor.detail}</strong>
      </div>
      <div className="factor-track" aria-hidden="true">
        <span
          className={`factor-fill factor-fill--${factor.tone}`}
          style={{ width: `${Math.max(factor.value * 2.15, 10)}%` }}
        />
      </div>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="empty-state">
      <Search size={24} />
      <strong>Сигналы не найдены</strong>
      <p>Измени запрос или выбери другой фильтр направления.</p>
    </div>
  );
}

export default function App() {
  const [selectedTicker, setSelectedTicker] = useState("SBER");
  const [filter, setFilter] = useState("all");
  const [query, setQuery] = useState("");
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  const filteredSignals = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase("ru-RU");
    return signals.filter((signal) => {
      const matchesDirection = filter === "all" || signal.direction === filter;
      const matchesQuery =
        !normalizedQuery ||
        signal.ticker.toLocaleLowerCase("ru-RU").includes(normalizedQuery) ||
        signal.company.toLocaleLowerCase("ru-RU").includes(normalizedQuery);
      return matchesDirection && matchesQuery;
    });
  }, [filter, query]);

  const selectedSignal =
    signals.find((signal) => signal.ticker === selectedTicker) ?? filteredSignals[0] ?? signals[0];
  const selectedMeta = directionMeta[selectedSignal.direction];

  return (
    <div className="app-shell">
      <aside className={`sidebar ${mobileNavOpen ? "is-open" : ""}`}>
        <div className="sidebar__brand">
          <BrandMark />
          <span>EventEdge</span>
          <button
            type="button"
            className="icon-button sidebar__close"
            onClick={() => setMobileNavOpen(false)}
            aria-label="Закрыть меню"
          >
            <X size={18} />
          </button>
        </div>

        <nav className="sidebar__nav" aria-label="Основная навигация">
          <p className="nav-label">Рабочее пространство</p>
          <button type="button" className="nav-item">
            <LayoutDashboard size={18} />
            Обзор
          </button>
          <button type="button" className="nav-item is-active">
            <CircleGauge size={18} />
            Сигналы
            <span className="nav-count">5</span>
          </button>
          <button type="button" className="nav-item">
            <BookOpenText size={18} />
            История
          </button>
          <p className="nav-label nav-label--spaced">Управление</p>
          <button type="button" className="nav-item">
            <Database size={18} />
            Источники
          </button>
          <button type="button" className="nav-item">
            <Settings2 size={18} />
            Модель
          </button>
        </nav>

        <div className="sidebar__system-card">
          <span className="system-status"><span /> Система работает</span>
          <strong>Данные обновлены</strong>
          <small>сегодня в 14:32 МСК</small>
        </div>

        <div className="sidebar__profile">
          <span className="profile-avatar">AR</span>
          <span>
            <strong>Алексей</strong>
            <small>Аналитик</small>
          </span>
          <Settings2 size={16} />
        </div>
      </aside>

      {mobileNavOpen && (
        <button
          type="button"
          className="sidebar-backdrop"
          aria-label="Закрыть меню"
          onClick={() => setMobileNavOpen(false)}
        />
      )}

      <main className="workspace">
        <header className="topbar">
          <button
            type="button"
            className="icon-button mobile-menu"
            onClick={() => setMobileNavOpen(true)}
            aria-label="Открыть меню"
          >
            <Menu size={19} />
          </button>
          <div className="topbar__title">
            <span>Российский рынок</span>
            <strong>Сигналы</strong>
          </div>
          <div className="market-pulse">
            <span>IMOEX</span>
            <strong>2 918,4</strong>
            <small>+0,62%</small>
          </div>
          <button type="button" className="icon-button" aria-label="Уведомления">
            <Bell size={18} />
            <span className="notification-dot" />
          </button>
        </header>

        <section className="workspace__content">
          <div className="page-heading">
            <div>
              <div className="eyebrow"><Sparkles size={14} /> Рынок сегодня</div>
              <h1>Где появилась новая инвестиционная гипотеза</h1>
              <p>Сигналы объединяют события, данные компании и реакцию рынка.</p>
            </div>
            <div className="session-card">
              <span className="session-card__icon"><Activity size={18} /></span>
              <span>
                <small>Торговая сессия</small>
                <strong>Открыта · ещё 3 ч 18 мин</strong>
              </span>
            </div>
          </div>

          <div className="signal-layout">
            <section className="signal-board" aria-label="Рейтинг сигналов">
              <div className="signal-board__toolbar">
                <div className="filter-tabs" role="group" aria-label="Фильтр направления">
                  {filters.map((item) => (
                    <button
                      type="button"
                      key={item.value}
                      className={filter === item.value ? "is-active" : ""}
                      onClick={() => setFilter(item.value)}
                      aria-pressed={filter === item.value}
                    >
                      {item.label}
                      <span>
                        {item.value === "all"
                          ? signals.length
                          : signals.filter((signal) => signal.direction === item.value).length}
                      </span>
                    </button>
                  ))}
                </div>
                <label className="signal-search">
                  <Search size={16} />
                  <input
                    type="search"
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="Тикер или компания"
                    aria-label="Найти сигнал"
                  />
                  {query && (
                    <button type="button" onClick={() => setQuery("")} aria-label="Очистить поиск">
                      <X size={14} />
                    </button>
                  )}
                </label>
              </div>

              <div className="signal-table-head" aria-hidden="true">
                <span>Инструмент</span>
                <span>Сигнал</span>
                <span>Скор</span>
                <span>Уверенность</span>
                <span>Горизонт</span>
                <span>Цена</span>
                <span />
              </div>

              <div className="signal-list">
                {filteredSignals.length ? (
                  filteredSignals.map((signal) => (
                    <SignalRow
                      key={signal.ticker}
                      signal={signal}
                      active={selectedSignal.ticker === signal.ticker}
                      onSelect={() => setSelectedTicker(signal.ticker)}
                    />
                  ))
                ) : (
                  <EmptyState />
                )}
              </div>

              <div className="signal-board__footer">
                <ShieldCheck size={15} />
                <span>Последний полный расчёт: 14:30 · модель baseline 0.1.0</span>
                <button type="button">Методология <ExternalLink size={13} /></button>
              </div>
            </section>

            <aside className={`signal-detail signal-detail--${selectedSignal.direction}`}>
              <div className="signal-detail__topline">
                <div className="detail-company">
                  <span className={`ticker-mark ticker-mark--${selectedSignal.direction}`}>
                    {selectedSignal.ticker.slice(0, 2)}
                  </span>
                  <span>
                    <strong>{selectedSignal.ticker}</strong>
                    <small>{selectedSignal.company} · MOEX</small>
                  </span>
                </div>
                <span className="detail-updated">{selectedSignal.updated}</span>
              </div>

              <div className="signal-decision">
                <DirectionBadge direction={selectedSignal.direction} />
                <div className="signal-decision__score">
                  <span>{selectedSignal.score}</span>
                  <small>итоговый скор</small>
                </div>
                <h2>{selectedSignal.action}</h2>
                <p>{selectedSignal.summary}</p>
              </div>

              <div className="decision-metrics">
                <div>
                  <small>Уверенность</small>
                  <strong>{selectedSignal.confidence}%</strong>
                  <span>{selectedSignal.strength}</span>
                </div>
                <div>
                  <small>Горизонт</small>
                  <strong>{selectedSignal.horizon.split(" ")[0]}</strong>
                  <span>торговых дня</span>
                </div>
                <div>
                  <small>Текущая цена</small>
                  <strong>{selectedSignal.price}</strong>
                  <span className={`market-change market-change--${selectedSignal.direction}`}>
                    {selectedSignal.change} сегодня
                  </span>
                </div>
              </div>

              <section className="detail-section">
                <div className="detail-section__heading">
                  <span>
                    <Target size={16} />
                    Из чего состоит сигнал
                  </span>
                  <small>вклад в оценку</small>
                </div>
                <div className="factor-list">
                  {selectedSignal.factors.map((factor) => (
                    <FactorBar key={factor.label} factor={factor} />
                  ))}
                </div>
              </section>

              <section className="detail-section detail-section--evidence">
                <div className="detail-section__heading">
                  <span>
                    <FileText size={16} />
                    Что изменилось
                  </span>
                  <small>{selectedSignal.evidence.length} факта</small>
                </div>
                <div className="evidence-list">
                  {selectedSignal.evidence.map((item, index) => (
                    <article className="evidence-item" key={`${item.time}-${item.title}`}>
                      <span className="evidence-item__line" aria-hidden="true">
                        <span>{index + 1}</span>
                      </span>
                      <div>
                        <div className="evidence-item__meta">
                          <span>{item.source}</span>
                          <span>{item.time}</span>
                          <span>{item.tag}</span>
                        </div>
                        <p>{item.title}</p>
                      </div>
                    </article>
                  ))}
                </div>
              </section>

              <div className={`action-card action-card--${selectedSignal.direction}`}>
                <span className="action-card__icon"><Check size={17} /></span>
                <div>
                  <small>{selectedMeta.actionLead}</small>
                  <strong>{selectedSignal.action}</strong>
                  <p>{selectedSignal.invalidation}</p>
                </div>
              </div>

              <p className="risk-note">
                Сигнал — аналитическая гипотеза, а не индивидуальная инвестиционная рекомендация.
              </p>
            </aside>
          </div>
        </section>
      </main>
    </div>
  );
}
