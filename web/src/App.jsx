import {
  ArrowDownRight,
  ArrowLeft,
  ArrowUpRight,
  Bell,
  Bookmark,
  Check,
  ChevronDown,
  CircleGauge,
  Clock3,
  Database,
  ExternalLink,
  FileText,
  Filter,
  LayoutGrid,
  Menu,
  Minus,
  Newspaper,
  Search,
  Settings,
  ShieldCheck,
  SlidersHorizontal,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

const signals = [
  {
    ticker: "SBER",
    company: "Сбербанк",
    sector: "Финансы",
    direction: "up",
    score: 42.7,
    confidence: 76,
    event: "Отчётность",
    horizon: "3 дня",
    price: "317,42 ₽",
    change: "+1,80%",
    updated: "12 мин",
    action: "Рассмотреть покупку",
    summary:
      "Свежая отчётность оказалась сильнее ожиданий, а текущая реакция рынка пока не отражает эффект полностью.",
    invalidation: "Пересмотреть идею при ухудшении маржи или закрытии дня ниже 308 ₽.",
    factors: [
      { label: "Новости", value: 38, tone: "positive" },
      { label: "Фундаментал", value: 27, tone: "positive" },
      { label: "Цена и объём", value: 21, tone: "positive" },
      { label: "Рыночный риск", value: 8, tone: "negative" },
    ],
    evidence: [
      { source: "Интерфакс", time: "10:18", tag: "Новый факт", title: "Чистая прибыль за семь месяцев выросла быстрее консенсуса" },
      { source: "Отчётность эмитента", time: "09:52", tag: "Первичный источник", title: "Рентабельность капитала удержалась выше целевого уровня" },
      { source: "MOEX", time: "10:34", tag: "Подтверждение", title: "Объём выше среднего, ценовая реакция пока остаётся умеренной" },
    ],
    series: [190, 191, 189, 190, 188, 187, 189, 188, 191, 193, 190, 188, 191, 194, 196, 195, 198, 199, 197, 201, 203, 202, 206, 205, 208, 210, 213, 211, 216, 218, 222, 220, 225],
  },
  {
    ticker: "LKOH",
    company: "Лукойл",
    sector: "Нефть и газ",
    direction: "up",
    score: 29.4,
    confidence: 69,
    event: "Дивиденды",
    horizon: "5 дней",
    price: "6 714 ₽",
    change: "+0,70%",
    updated: "28 мин",
    action: "Добавить в наблюдение",
    summary: "Денежный поток и дивидендные ожидания поддерживают идею, но часть эффекта уже находится в цене.",
    invalidation: "Сигнал станет нейтральным при снижении нефти ниже контрольного уровня.",
    factors: [
      { label: "Новости", value: 24, tone: "positive" },
      { label: "Фундаментал", value: 31, tone: "positive" },
      { label: "Цена и объём", value: 9, tone: "positive" },
      { label: "Учтено в цене", value: 13, tone: "negative" },
    ],
    evidence: [
      { source: "Отчётность эмитента", time: "09:40", tag: "Первичный источник", title: "Свободный денежный поток превысил прошлогодний уровень" },
      { source: "Интерфакс", time: "10:01", tag: "Ожидания", title: "Рынок пересматривает оценку дивидендной доходности" },
      { source: "MOEX", time: "10:10", tag: "Цена", title: "Бумага уже росла в предыдущие две торговые сессии" },
    ],
    series: [172, 174, 173, 176, 175, 178, 180, 179, 181, 184, 183, 186, 189, 188, 192, 190, 193, 194, 198, 197, 200, 201, 199, 203, 205, 204, 208, 207, 210],
  },
  {
    ticker: "YDEX",
    company: "Яндекс",
    sector: "Технологии",
    direction: "neutral",
    score: 7.1,
    confidence: 58,
    event: "Продукт",
    horizon: "3 дня",
    price: "4 168 ₽",
    change: "+0,20%",
    updated: "41 мин",
    action: "Ждать подтверждения",
    summary: "Позитивные операционные новости компенсируются высокой оценкой и слабым подтверждением торговым объёмом.",
    invalidation: "Направленный сигнал появится после нового факта или подтверждения ценой и объёмом.",
    factors: [
      { label: "Новости", value: 19, tone: "positive" },
      { label: "Фундаментал", value: 15, tone: "positive" },
      { label: "Оценка", value: 18, tone: "negative" },
      { label: "Цена и объём", value: 9, tone: "negative" },
    ],
    evidence: [
      { source: "РБК", time: "09:14", tag: "Событие", title: "Компания анонсировала расширение облачного направления" },
      { source: "Компания", time: "09:26", tag: "Первичный источник", title: "Опубликованы детали продуктового запуска" },
      { source: "MOEX", time: "10:02", tag: "Нет подтверждения", title: "Объём торгов ниже двадцатидневного среднего" },
    ],
    series: [202, 200, 203, 205, 204, 206, 205, 207, 209, 207, 206, 208, 207, 209, 210, 209, 211, 210, 211, 210, 212, 211, 212],
  },
  {
    ticker: "NVTK",
    company: "Новатэк",
    sector: "Нефть и газ",
    direction: "down",
    score: -36.8,
    confidence: 73,
    event: "Ограничения",
    horizon: "5 дней",
    price: "1 081 ₽",
    change: "−2,10%",
    updated: "1 ч",
    action: "Сократить риск",
    summary: "Новые ограничения повышают неопределённость по срокам проектов, а рынок подтверждает ухудшение ожиданий.",
    invalidation: "Официальное подтверждение неизменных сроков проекта отменит текущую гипотезу.",
    factors: [
      { label: "Новости", value: 35, tone: "negative" },
      { label: "Цена и объём", value: 22, tone: "negative" },
      { label: "Риск исполнения", value: 18, tone: "negative" },
      { label: "Баланс", value: 12, tone: "positive" },
    ],
    evidence: [
      { source: "Reuters", time: "08:57", tag: "Высокий эффект", title: "Обновлены ограничения для поставок технологического оборудования" },
      { source: "Интерфакс", time: "09:08", tag: "Контекст", title: "Аналитики оценивают возможный сдвиг сроков проекта" },
      { source: "MOEX", time: "09:27", tag: "Подтверждение", title: "Снижение подтверждено объёмом выше среднего" },
    ],
    series: [228, 227, 230, 226, 225, 223, 224, 221, 219, 220, 217, 216, 218, 215, 212, 214, 211, 209, 210, 206, 205, 203, 201, 202, 198, 196, 194],
  },
  {
    ticker: "TATN",
    company: "Татнефть",
    sector: "Нефть и газ",
    direction: "up",
    score: 24.2,
    confidence: 66,
    event: "Производство",
    horizon: "5 дней",
    price: "694,8 ₽",
    change: "+1,10%",
    updated: "1 ч",
    action: "Следить за продолжением",
    summary: "Производственные показатели улучшились, но для сильного сигнала не хватает подтверждения следующим отчётом.",
    invalidation: "Слабая динамика добычи в следующем обновлении вернёт сигнал в нейтральную зону.",
    factors: [
      { label: "Новости", value: 21, tone: "positive" },
      { label: "Фундаментал", value: 18, tone: "positive" },
      { label: "Цена и объём", value: 12, tone: "positive" },
      { label: "Отраслевой риск", value: 9, tone: "negative" },
    ],
    evidence: [
      { source: "Компания", time: "08:46", tag: "Первичный источник", title: "Опубликованы операционные показатели за месяц" },
      { source: "ТАСС", time: "09:05", tag: "Контекст", title: "Добыча превысила средний темп первого полугодия" },
      { source: "MOEX", time: "09:48", tag: "Цена", title: "Покупатели удерживают цену выше открытия" },
    ],
    series: [190, 192, 191, 194, 193, 195, 197, 196, 198, 200, 199, 201, 204, 203, 206, 205, 207, 209, 211, 210, 213],
  },
  {
    ticker: "ROSN",
    company: "Роснефть",
    sector: "Нефть и газ",
    direction: "neutral",
    score: -3.6,
    confidence: 54,
    event: "Рынок",
    horizon: "3 дня",
    price: "482,7 ₽",
    change: "−0,30%",
    updated: "2 ч",
    action: "Ничего не делать",
    summary: "Положительный денежный поток уравновешен слабым отраслевым фоном — статистического преимущества сейчас нет.",
    invalidation: "Новая отчётность или сильное движение нефти изменит баланс факторов.",
    factors: [
      { label: "Новости", value: 7, tone: "negative" },
      { label: "Фундаментал", value: 14, tone: "positive" },
      { label: "Цена и объём", value: 5, tone: "negative" },
      { label: "Отраслевой фон", value: 8, tone: "negative" },
    ],
    evidence: [
      { source: "Интерфакс", time: "08:21", tag: "Повтор", title: "Новость не содержит новых параметров относительно прошлого раскрытия" },
      { source: "Компания", time: "08:45", tag: "Документ", title: "Опубликован плановый корпоративный материал" },
      { source: "MOEX", time: "09:31", tag: "Нет реакции", title: "Выраженной реакции цены и объёма нет" },
    ],
    series: [208, 207, 209, 208, 207, 206, 208, 207, 209, 208, 207, 208, 206, 207, 208, 207, 206, 207],
  },
  {
    ticker: "GMKN",
    company: "Норникель",
    sector: "Металлы",
    direction: "down",
    score: -22.9,
    confidence: 64,
    event: "Сырьё",
    horizon: "3 дня",
    price: "123,54 ₽",
    change: "−1,20%",
    updated: "2 ч",
    action: "Не открывать позицию",
    summary: "Снижение цен на металлы и слабая реакция покупателей создают отрицательное краткосрочное ожидание.",
    invalidation: "Возврат цены металлов и бумаги выше недельного максимума отменит сигнал.",
    factors: [
      { label: "Новости", value: 14, tone: "negative" },
      { label: "Сырьевой фактор", value: 23, tone: "negative" },
      { label: "Цена и объём", value: 16, tone: "negative" },
      { label: "Оценка", value: 11, tone: "positive" },
    ],
    evidence: [
      { source: "Reuters", time: "07:58", tag: "Рынок сырья", title: "Промышленные металлы снижаются на азиатской сессии" },
      { source: "Интерфакс", time: "08:32", tag: "Контекст", title: "Экспортные цены остаются под давлением" },
      { source: "MOEX", time: "09:44", tag: "Подтверждение", title: "Попытка восстановления не поддержана объёмом" },
    ],
    series: [226, 224, 225, 222, 220, 221, 219, 216, 218, 214, 213, 215, 212, 210, 209, 207, 208, 205, 203],
  },
  {
    ticker: "MGNT",
    company: "Магнит",
    sector: "Ритейл",
    direction: "neutral",
    score: -4.3,
    confidence: 52,
    event: "Стратегия",
    horizon: "3 дня",
    price: "4 706 ₽",
    change: "−0,30%",
    updated: "3 ч",
    action: "Ждать операционных данных",
    summary: "Информационный фон противоречив, а свежих данных недостаточно для решения с преимуществом.",
    invalidation: "Сигнал изменится после публикации операционных результатов.",
    factors: [
      { label: "Новости", value: 8, tone: "negative" },
      { label: "Фундаментал", value: 12, tone: "positive" },
      { label: "Цена и объём", value: 5, tone: "negative" },
      { label: "Качество данных", value: 7, tone: "negative" },
    ],
    evidence: [
      { source: "ТАСС", time: "08:20", tag: "Повтор", title: "Компания обновила планы открытия магазинов" },
      { source: "Компания", time: "08:38", tag: "Комментарий", title: "Новых численных ориентиров не опубликовано" },
      { source: "MOEX", time: "09:31", tag: "Нет реакции", title: "Выраженной реакции цены и объёма нет" },
    ],
    series: [205, 204, 206, 205, 203, 204, 202, 203, 204, 203, 202, 204, 203, 202, 201, 202, 201],
  },
];

const directionMeta = {
  up: { label: "Вверх", Icon: ArrowUpRight },
  neutral: { label: "Нейтрально", Icon: Minus },
  down: { label: "Вниз", Icon: ArrowDownRight },
};

const companyColors = {
  SBER: "#24a16a",
  LKOH: "#d94d52",
  YDEX: "#f2b84b",
  NVTK: "#3779d6",
  TATN: "#4c947d",
  ROSN: "#d2b231",
  GMKN: "#507b9e",
  MGNT: "#dc4e5f",
};

function BrandMark() {
  return <span className="brand-mark">E</span>;
}

function CompanyMark({ signal, small = false }) {
  return (
    <span
      className={`company-mark ${small ? "company-mark--small" : ""}`}
      style={{ "--company-color": companyColors[signal.ticker] }}
      aria-hidden="true"
    >
      {signal.ticker.slice(0, 1)}
    </span>
  );
}

function Direction({ direction }) {
  const meta = directionMeta[direction];
  const Icon = meta.Icon;
  return (
    <span className={`direction direction--${direction}`}>
      <Icon size={13} strokeWidth={2.2} />
      {meta.label}
    </span>
  );
}

function PriceChart({ signal }) {
  const ref = useRef(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return undefined;

    const draw = () => {
      const rect = canvas.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.round(rect.width * dpr);
      canvas.height = Math.round(rect.height * dpr);
      const context = canvas.getContext("2d");
      context.setTransform(dpr, 0, 0, dpr, 0, 0);
      context.clearRect(0, 0, rect.width, rect.height);

      const padding = { top: 24, right: 24, bottom: 24, left: 24 };
      const chartWidth = rect.width - padding.left - padding.right;
      const chartHeight = rect.height - padding.top - padding.bottom;
      const min = Math.min(...signal.series);
      const max = Math.max(...signal.series);
      const spread = Math.max(max - min, 1);
      const points = signal.series.map((value, index) => ({
        x: padding.left + (index / (signal.series.length - 1)) * chartWidth,
        y: padding.top + (1 - (value - min) / spread) * chartHeight,
      }));

      context.fillStyle = "rgba(115, 121, 133, 0.18)";
      for (let x = padding.left; x < rect.width - padding.right; x += 12) {
        for (let y = padding.top; y < rect.height - padding.bottom; y += 12) {
          context.fillRect(x, y, 1, 1);
        }
      }

      const baselineY = points[Math.floor(points.length * 0.28)].y;
      context.save();
      context.setLineDash([3, 5]);
      context.strokeStyle = "rgba(139, 145, 158, 0.24)";
      context.lineWidth = 1;
      context.beginPath();
      context.moveTo(points[Math.floor(points.length * 0.28)].x, padding.top);
      context.lineTo(points[Math.floor(points.length * 0.28)].x, rect.height - padding.bottom);
      context.stroke();
      context.restore();

      const lineGradient = context.createLinearGradient(padding.left, 0, rect.width - padding.right, 0);
      lineGradient.addColorStop(0, "#5d626d");
      lineGradient.addColorStop(0.36, "#7e78c8");
      lineGradient.addColorStop(0.72, signal.direction === "down" ? "#df7789" : signal.direction === "neutral" ? "#aaa5ba" : "#dac2dc");
      lineGradient.addColorStop(1, signal.direction === "down" ? "#e87486" : signal.direction === "neutral" ? "#b9b6c4" : "#f0bca8");

      context.beginPath();
      points.forEach((point, index) => {
        if (index === 0) context.moveTo(point.x, point.y);
        else context.lineTo(point.x, point.y);
      });
      context.lineWidth = 2;
      context.lineJoin = "round";
      context.lineCap = "round";
      context.strokeStyle = lineGradient;
      context.stroke();

      const last = points.at(-1);
      context.fillStyle = signal.direction === "down" ? "#e87486" : signal.direction === "neutral" ? "#aaa5ba" : "#efb9a5";
      context.beginPath();
      context.arc(last.x, last.y, 3.4, 0, Math.PI * 2);
      context.fill();
      context.strokeStyle = signal.direction === "down" ? "rgba(232,116,134,.22)" : "rgba(239,185,165,.2)";
      context.lineWidth = 8;
      context.stroke();

      context.fillStyle = "rgba(151, 157, 169, 0.58)";
      context.font = "10px ui-sans-serif, system-ui";
      context.fillText("Предыдущее закрытие", Math.max(points[Math.floor(points.length * 0.28)].x - 55, 8), Math.max(baselineY - 12, 14));
    };

    draw();
    const observer = new ResizeObserver(draw);
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [signal]);

  return <canvas className="price-chart" ref={ref} aria-label={`Динамика цены ${signal.ticker}`} />;
}

function AppHeader({ onHome, onSelect }) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const searchResults = signals.filter((signal) =>
    `${signal.ticker} ${signal.company}`.toLocaleLowerCase("ru-RU").includes(searchQuery.trim().toLocaleLowerCase("ru-RU")),
  );

  useEffect(() => {
    const handleShortcut = (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLocaleLowerCase() === "k") {
        event.preventDefault();
        setSearchOpen(true);
      }
      if (event.key === "Escape") setSearchOpen(false);
    };
    window.addEventListener("keydown", handleShortcut);
    return () => window.removeEventListener("keydown", handleShortcut);
  }, []);

  const chooseResult = (ticker) => {
    onSelect(ticker);
    setSearchOpen(false);
    setSearchQuery("");
  };

  return (
    <>
      <header className="app-header">
        <button className="brand" type="button" onClick={onHome} aria-label="EventEdge — на главную">
          <BrandMark />
          <span>EventEdge</span>
        </button>

        <nav className={`primary-nav ${mobileOpen ? "is-open" : ""}`} aria-label="Основная навигация">
          <button type="button" className="is-active" onClick={onHome}><CircleGauge size={14} /> Сигналы</button>
          <button type="button"><Newspaper size={14} /> Новости</button>
          <button type="button"><Database size={14} /> Источники</button>
          <button type="button"><Settings size={14} /> Настройки</button>
        </nav>

        <div className="header-actions">
          <button className="command-search" type="button" onClick={() => setSearchOpen(true)}>
            <Search size={15} />
            <span>Найти компанию</span>
            <kbd>⌘ K</kbd>
          </button>
          <button className="icon-button" type="button" aria-label="Уведомления">
            <Bell size={17} />
            <span className="notification-dot" />
          </button>
          <button className="icon-button menu-button" type="button" aria-label="Открыть меню" onClick={() => setMobileOpen((value) => !value)}>
            {mobileOpen ? <X size={18} /> : <Menu size={18} />}
          </button>
          <span className="profile-dot">AR</span>
        </div>
      </header>

      {searchOpen && (
        <div className="search-overlay">
          <button className="overlay-dismiss" type="button" aria-label="Закрыть поиск" onClick={() => setSearchOpen(false)} />
          <section className="search-dialog" role="dialog" aria-modal="true" aria-label="Поиск компании">
            <div className="search-dialog__input">
              <Search size={17} />
              <input autoFocus value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} placeholder="Поиск по тикеру или названию" />
              <kbd>ESC</kbd>
            </div>
            <div className="search-dialog__label">Компании российского рынка</div>
            <div className="search-results">
              {searchResults.slice(0, 6).map((signal, index) => (
                <button type="button" key={signal.ticker} className={index === 0 ? "is-active" : ""} onClick={() => chooseResult(signal.ticker)}>
                  <CompanyMark signal={signal} small />
                  <span><strong>{signal.ticker}</strong><small>{signal.company} · {signal.sector}</small></span>
                  <Direction direction={signal.direction} />
                  <em>{signal.price}</em>
                </button>
              ))}
            </div>
            <footer><span>↵ открыть</span><span>↑↓ выбрать</span><span>⌘ K поиск</span></footer>
          </section>
        </div>
      )}
    </>
  );
}

function SignalsScreen({ onSelect }) {
  const [filter, setFilter] = useState("all");
  const [query, setQuery] = useState("");
  const [filterOpen, setFilterOpen] = useState(false);
  const [sortByConfidence, setSortByConfidence] = useState(false);

  const filteredSignals = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase("ru-RU");
    const result = signals.filter((signal) => {
      const matchesFilter = filter === "all" || signal.direction === filter;
      const matchesQuery = !normalized || `${signal.ticker} ${signal.company}`.toLocaleLowerCase("ru-RU").includes(normalized);
      return matchesFilter && matchesQuery;
    });
    if (sortByConfidence) return [...result].sort((a, b) => b.confidence - a.confidence);
    return result;
  }, [filter, query, sortByConfidence]);

  return (
    <main className="screen screen--signals">
      <section className="terminal-window">
        <div className="terminal-toolbar">
          <div className="terminal-title">
            <h1>Сигналы рынка</h1>
            <div className="filter-wrap">
              <button type="button" className={`text-button ${filter !== "all" ? "is-active" : ""}`} onClick={() => setFilterOpen((value) => !value)}>
                <Filter size={14} />
                {filter === "all" ? "Добавить фильтр" : directionMeta[filter].label}
                <ChevronDown size={12} />
              </button>
              {filterOpen && (
                <div className="filter-menu">
                  {[
                    ["all", "Все сигналы"],
                    ["up", "Вверх"],
                    ["neutral", "Нейтрально"],
                    ["down", "Вниз"],
                  ].map(([value, label]) => (
                    <button key={value} type="button" className={filter === value ? "is-active" : ""} onClick={() => { setFilter(value); setFilterOpen(false); }}>
                      {label}
                      {filter === value && <Check size={13} />}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>

          <div className="terminal-actions">
            <label className="inline-search">
              <Search size={14} />
              <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Тикер или компания" aria-label="Найти сигнал" />
              {query && <button type="button" onClick={() => setQuery("")} aria-label="Очистить поиск"><X size={13} /></button>}
            </label>
            <button className={`square-button ${sortByConfidence ? "is-active" : ""}`} type="button" aria-label="Сортировать по уверенности" onClick={() => setSortByConfidence((value) => !value)}>
              <SlidersHorizontal size={15} />
            </button>
            <button className="square-button" type="button" aria-label="Табличный вид">
              <LayoutGrid size={15} />
            </button>
          </div>
        </div>

        <div className="market-strip">
          <span><i /> MOEX открыт</span>
          <span>IMOEX <strong>2 918,4</strong> <em>+0,62%</em></span>
          <span>Последний расчёт <strong>14:30 МСК</strong></span>
          <span className="market-strip__right">{filteredSignals.length} из {signals.length} бумаг</span>
        </div>

        <div className="signals-table">
          <div className="signal-grid table-head" aria-hidden="true">
            <span>Компания</span>
            <span>Сектор</span>
            <span>Сигнал</span>
            <span>Скор</span>
            <span>Событие</span>
            <span>Уверенность</span>
            <span>Горизонт</span>
            <span>Цена</span>
            <span>День</span>
            <span>Обновлено</span>
          </div>

          <div className="table-body">
            {filteredSignals.map((signal) => (
              <button className="signal-grid table-row" type="button" key={signal.ticker} onClick={() => onSelect(signal.ticker)}>
                <span className="company-cell">
                  <CompanyMark signal={signal} small />
                  <span><strong>{signal.ticker}</strong><small>{signal.company}</small></span>
                </span>
                <span className="sector-cell">{signal.sector}</span>
                <Direction direction={signal.direction} />
                <span className={`score-cell score-cell--${signal.direction}`}>{signal.score > 0 ? "+" : ""}{signal.score.toFixed(1)}</span>
                <span className="event-cell">{signal.event}</span>
                <span className="confidence-cell"><i><b style={{ width: `${signal.confidence}%` }} /></i>{signal.confidence}%</span>
                <span className="horizon-cell">{signal.horizon}</span>
                <span className="price-cell">{signal.price}</span>
                <span className={`change-cell change-cell--${signal.direction}`}>{signal.change}</span>
                <span className="updated-cell">{signal.updated}</span>
              </button>
            ))}
          </div>

          {!filteredSignals.length && (
            <div className="empty-state">
              <Search size={22} />
              <strong>Сигналы не найдены</strong>
              <span>Измени запрос или фильтр направления.</span>
            </div>
          )}
        </div>

        <footer className="terminal-footer">
          <span><ShieldCheck size={13} /> Baseline 0.1.0 · данные проверены</span>
          <button type="button">Как считается сигнал <ExternalLink size={12} /></button>
        </footer>
      </section>
    </main>
  );
}

function CompanyScreen({ signal, onBack }) {
  const [bookmarked, setBookmarked] = useState(false);
  const [range, setRange] = useState("1Д");
  const [selectedEvidence, setSelectedEvidence] = useState(null);

  return (
    <main className="screen screen--company">
      <div className="company-header">
        <button className="back-button" type="button" onClick={onBack} aria-label="Вернуться к сигналам"><ArrowLeft size={19} /></button>
        <CompanyMark signal={signal} />
        <div className="company-identity">
          <strong>{signal.ticker}</strong>
          <span>{signal.company}</span>
        </div>
        <div className="company-tabs" role="tablist" aria-label="Разделы компании">
          <button type="button">График</button>
          <button type="button" className="is-active">Сигнал</button>
          <button type="button">Новости</button>
          <button type="button">Показатели</button>
          <button type="button">История</button>
        </div>
        <button className={`bookmark-button ${bookmarked ? "is-active" : ""}`} type="button" onClick={() => setBookmarked((value) => !value)} aria-label={bookmarked ? "Убрать из избранного" : "Добавить в избранное"}>
          <Bookmark size={17} fill={bookmarked ? "currentColor" : "none"} />
        </button>
      </div>

      <section className="company-canvas">
        <div className="chart-card">
          <div className="chart-summary">
            <div>
              <span>MOEX · {signal.company}</span>
              <strong>{signal.price}</strong>
              <em className={`change-cell--${signal.direction}`}>{signal.change}</em>
            </div>
            <div className="chart-signal">
              <Direction direction={signal.direction} />
              <span>на горизонте {signal.horizon}</span>
            </div>
          </div>
          <PriceChart signal={signal} />
          <div className="range-tabs" role="group" aria-label="Период графика">
            {["1Д", "1Н", "1М", "3М", "Год", "Всё"].map((item) => (
              <button key={item} type="button" className={range === item ? "is-active" : ""} onClick={() => setRange(item)}>{item}</button>
            ))}
          </div>
        </div>

        <div className="analysis-grid">
          <section className="decision-card">
            <div className="section-kicker"><CircleGauge size={14} /> Решение модели</div>
            <div className="decision-headline">
              <div>
                <h1>{signal.action}</h1>
                <p>{signal.summary}</p>
              </div>
              <div className="decision-score">
                <strong className={`score-cell--${signal.direction}`}>{signal.score > 0 ? "+" : ""}{signal.score.toFixed(1)}</strong>
                <span>итоговый скор</span>
              </div>
            </div>
            <div className="decision-stats">
              <div><span>Уверенность</span><strong>{signal.confidence}%</strong><small>{signal.confidence >= 70 ? "высокая" : signal.confidence >= 60 ? "средняя" : "ограниченная"}</small></div>
              <div><span>Горизонт</span><strong>{signal.horizon}</strong><small>торговых</small></div>
              <div><span>Событие</span><strong>{signal.event}</strong><small>главный драйвер</small></div>
            </div>
            <div className={`action-note action-note--${signal.direction}`}>
              <Check size={15} />
              <div><strong>Что делать инвестору</strong><span>{signal.action}. {signal.invalidation}</span></div>
            </div>
          </section>

          <section className="factors-card">
            <div className="section-heading">
              <span><SlidersHorizontal size={14} /> Из чего состоит сигнал</span>
              <small>вклад факторов</small>
            </div>
            <div className="factor-list">
              {signal.factors.map((factor) => (
                <div className="factor-row" key={factor.label}>
                  <div><span>{factor.label}</span><strong className={`factor--${factor.tone}`}>{factor.tone === "positive" ? "+" : "−"}{factor.value}</strong></div>
                  <i><b className={`factor--${factor.tone}`} style={{ width: `${Math.min(factor.value * 2.2, 100)}%` }} /></i>
                </div>
              ))}
            </div>
            <button className="method-link" type="button"><Database size={13} /> Открыть расчёт модели <ExternalLink size={11} /></button>
          </section>
        </div>

        <section className="news-card">
          <div className="section-heading">
            <span><Newspaper size={15} /> Новости и данные, изменившие сигнал</span>
            <button type="button">Все источники <ExternalLink size={11} /></button>
          </div>
          <div className="news-list">
            {signal.evidence.map((item, index) => (
              <article className="news-item" key={`${item.source}-${item.time}`}>
                <span className="news-index">0{index + 1}</span>
                <div className="news-content">
                  <div><span>{item.source}</span><i>{item.tag}</i></div>
                  <h2>{item.title}</h2>
                  <p>Факт извлечён и сопоставлен с компанией. Вклад проверен на свежесть, релевантность и реакцию рынка.</p>
                </div>
                <span className="news-time"><Clock3 size={12} /> {item.time}</span>
                <button type="button" aria-label="Открыть источники" onClick={() => setSelectedEvidence(item)}><ExternalLink size={14} /></button>
              </article>
            ))}
          </div>
        </section>

        <p className="disclaimer">Сигнал является аналитической гипотезой и не является индивидуальной инвестиционной рекомендацией.</p>
      </section>

      {selectedEvidence && (
        <div className="sources-overlay">
          <button className="overlay-dismiss" type="button" aria-label="Закрыть источники" onClick={() => setSelectedEvidence(null)} />
          <section className="sources-dialog" role="dialog" aria-modal="true" aria-label="Источники новости">
            <header>
              <span><CompanyMark signal={signal} small /> {signal.ticker} · MOEX</span>
              <button type="button" onClick={() => setSelectedEvidence(null)} aria-label="Закрыть"><X size={16} /></button>
            </header>
            <div className="sources-query">{selectedEvidence.title}</div>
            <div className="sources-label">Источники события</div>
            {[
              [selectedEvidence.source, selectedEvidence.title, selectedEvidence.time],
              ["РБК Инвестиции", `Рынок оценивает событие вокруг ${signal.company}`, "10:26"],
              ["Коммерсантъ", `${signal.company}: ключевые факты и реакция участников рынка`, "10:41"],
            ].map(([source, title, time], index) => (
              <button type="button" className={index === 0 ? "is-active" : ""} key={`${source}-${time}`}>
                <FileText size={14} />
                <span><strong>{title}</strong><small>{source} · сегодня, {time}</small></span>
                <ExternalLink size={13} />
              </button>
            ))}
            <footer><ShieldCheck size={13} /> Событие объединено из 3 публикаций без повторного учёта в сигнале</footer>
          </section>
        </div>
      )}
    </main>
  );
}

export default function App() {
  const [selectedTicker, setSelectedTicker] = useState(null);
  const selectedSignal = signals.find((signal) => signal.ticker === selectedTicker);

  return (
    <div className="app-shell">
      <AppHeader onHome={() => setSelectedTicker(null)} onSelect={setSelectedTicker} />
      {selectedSignal ? (
        <CompanyScreen signal={selectedSignal} onBack={() => setSelectedTicker(null)} />
      ) : (
        <SignalsScreen onSelect={setSelectedTicker} />
      )}
    </div>
  );
}
