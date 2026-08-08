import {
  ArrowDownRight,
  ArrowLeft,
  ArrowUpRight,
  BookOpen,
  Braces,
  Check,
  ChevronDown,
  CircleGauge,
  Clock3,
  Copy,
  Database,
  FileText,
  Filter,
  Info,
  Menu,
  Minus,
  Newspaper,
  RefreshCw,
  Search,
  Server,
  ShieldCheck,
  SlidersHorizontal,
  Terminal,
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

const companyBrands = {
  SBER: { colors: ["#21a366", "#0c7650"], glyph: "С", domain: "sberbank.ru" },
  LKOH: { colors: ["#ee2d33", "#9c101d"], glyph: "Л", domain: "lukoil.ru" },
  YDEX: { colors: ["#ffcc00", "#f04b3f"], glyph: "Я", domain: "yandex.ru" },
  NVTK: { colors: ["#2863b3", "#173f7d"], glyph: "N", domain: "novatek.ru" },
  TATN: { colors: ["#008d6b", "#006047"], glyph: "Т", domain: "tatneft.ru" },
  ROSN: { colors: ["#f2c500", "#111216"], glyph: "Р", domain: "rosneft.ru" },
  GMKN: { colors: ["#1f7ca8", "#124b72"], glyph: "Н", domain: "nornickel.ru" },
  MGNT: { colors: ["#ef3340", "#a71930"], glyph: "М", domain: "magnit.com" },
};

const scoreFactorDefinitions = [
  { label: "Текстовый эффект", weight: 0.55, description: "Как событие меняет ожидания по компании" },
  { label: "Существенность", weight: 0.2, description: "Насколько событие способно повлиять на стоимость" },
  { label: "Новизна", weight: 0.1, description: "Есть ли в публикации действительно новый факт" },
  { label: "Качество источника", weight: 0.1, description: "Надёжность и близость источника к первичным данным" },
  { label: "Полнота фактов", weight: 0.05, description: "Достаточно ли чисел, сроков и подтверждений" },
];

function scoreFactors(score) {
  let allocated = 0;
  return scoreFactorDefinitions.map((factor, index) => {
    const contribution = index === scoreFactorDefinitions.length - 1
      ? Number((score - allocated).toFixed(1))
      : Number((score * factor.weight).toFixed(1));
    allocated = Number((allocated + contribution).toFixed(1));
    return { ...factor, contribution };
  });
}

function formatScore(score) {
  return `${score > 0 ? "+" : ""}${score.toFixed(1)}`;
}

const allNews = signals.flatMap((signal) =>
  signal.evidence.map((item, index) => ({
    ...item,
    id: `${signal.ticker}-${index}`,
    signal,
    index,
  })),
);

function BrandMark() {
  return (
    <span className="brand-mark" aria-hidden="true">
      <i className="brand-mark__axis" />
      <i className="brand-mark__up" />
      <i className="brand-mark__down" />
      <i className="brand-mark__dot" />
    </span>
  );
}

function CompanyMark({ signal, small = false }) {
  const [imageFailed, setImageFailed] = useState(false);
  const brand = companyBrands[signal.ticker];
  return (
    <span
      className={`company-mark ${small ? "company-mark--small" : ""}`}
      style={{ "--company-color": brand.colors[0], "--company-color-deep": brand.colors[1] }}
    >
      {!imageFailed && (
        <img
          src={`https://${brand.domain}/favicon.ico`}
          alt=""
          loading="lazy"
          onError={() => setImageFailed(true)}
        />
      )}
      {imageFailed && <b aria-hidden="true">{brand.glyph}</b>}
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

function AppHeader({ view, onNavigate, onSelect }) {
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

  const navigate = (nextView) => {
    onNavigate(nextView);
    setMobileOpen(false);
  };

  return (
    <>
      <header className="app-header">
        <button className="brand" type="button" onClick={() => navigate("signals")} aria-label="EventEdge — на главную">
          <BrandMark />
          <span>EventEdge<small>market intelligence</small></span>
        </button>

        <nav className={`primary-nav ${mobileOpen ? "is-open" : ""}`} aria-label="Основная навигация">
          <button type="button" className={view === "signals" || view === "signal" ? "is-active" : ""} onClick={() => navigate("signals")}><CircleGauge size={14} /> Сигналы</button>
          <button type="button" className={view === "news" ? "is-active" : ""} onClick={() => navigate("news")}><Newspaper size={14} /> Новости</button>
          <button type="button" className={view === "methodology" ? "is-active" : ""} onClick={() => navigate("methodology")}><BookOpen size={14} /> Методика</button>
          <button type="button" className={view === "api" ? "is-active" : ""} onClick={() => navigate("api")}><Braces size={14} /> API</button>
        </nav>

        <div className="header-actions">
          <button className="command-search" type="button" onClick={() => setSearchOpen(true)}>
            <Search size={15} />
            <span>Найти компанию</span>
            <kbd>⌘ K</kbd>
          </button>
          <button className="icon-button menu-button" type="button" aria-label="Открыть меню" onClick={() => setMobileOpen((value) => !value)}>
            {mobileOpen ? <X size={18} /> : <Menu size={18} />}
          </button>
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
            <footer><span>Нажми на компанию, чтобы открыть сигнал</span><span>⌘ K — поиск</span></footer>
          </section>
        </div>
      )}
    </>
  );
}

function SignalsScreen({ onSelect, onMethodology }) {
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
            <button className={`sort-button ${sortByConfidence ? "is-active" : ""}`} type="button" aria-label="Сортировать по уверенности" onClick={() => setSortByConfidence((value) => !value)}>
              <SlidersHorizontal size={15} />
              <span>{sortByConfidence ? "По уверенности" : "Сортировка"}</span>
            </button>
          </div>
        </div>

        <div className="market-strip">
          <span><i /> Демонстрационный набор</span>
          <span>Baseline <strong>0.1.0</strong></span>
          <span>Шкала сигнала <strong>от −100 до +100</strong></span>
          <span className="market-strip__right">{filteredSignals.length} из {signals.length} бумаг</span>
        </div>

        <div className="signals-table">
          <div className="signal-grid table-head" aria-hidden="true">
            <span>Компания</span>
            <span>Сектор</span>
            <span>Сигнал</span>
            <span>Оценка</span>
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
                <span className={`score-cell score-cell--${signal.direction}`}>{formatScore(signal.score)}<small> п.</small></span>
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
          <span><ShieldCheck size={13} /> Пункты — это сила сигнала, а не прогноз доходности</span>
          <button type="button" onClick={onMethodology}>Как считается сигнал <ArrowUpRight size={12} /></button>
        </footer>
      </section>
    </main>
  );
}

function CompanyScreen({ signal, onBack, onMethodology, onOpenNews, onReadNews }) {
  const factors = scoreFactors(signal.score);

  return (
    <main className="screen screen--company">
      <div className="company-header">
        <button className="back-button" type="button" onClick={onBack} aria-label="Вернуться к сигналам"><ArrowLeft size={19} /></button>
        <CompanyMark signal={signal} />
        <div className="company-identity">
          <strong>{signal.ticker}</strong>
          <span>{signal.company} · MOEX</span>
        </div>
        <div className="company-header__signal"><Direction direction={signal.direction} /><span>{signal.horizon}</span></div>
      </div>

      <section className="company-canvas">
        <div className="chart-card">
          <div className="chart-summary">
            <div>
              <span>Динамика бумаги · демо-график за день</span>
              <strong>{signal.price}</strong>
              <em className={`change-cell--${signal.direction}`}>{signal.change}</em>
            </div>
            <div className="chart-signal">
              <Direction direction={signal.direction} />
              <span>ожидание на {signal.horizon}</span>
            </div>
          </div>
          <PriceChart signal={signal} />
        </div>

        <div className="analysis-grid">
          <section className="decision-card">
            <div className="section-kicker"><CircleGauge size={14} /> Аналитический сигнал</div>
            <div className="decision-headline">
              <div>
                <h1>{signal.action}</h1>
                <p>{signal.summary}</p>
              </div>
              <div className="decision-score">
                <strong className={`score-cell--${signal.direction}`}>{formatScore(signal.score)}</strong>
                <span>пунктов из 100</span>
              </div>
            </div>
            <button className="score-explainer" type="button" onClick={onMethodology}>
              <Info size={14} />
              <span><strong>Что означают пункты?</strong> Это сила и направление гипотезы, не ожидаемая доходность.</span>
              <ArrowUpRight size={13} />
            </button>
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
              <span><SlidersHorizontal size={14} /> Из чего состоит оценка</span>
              <small>вклад в пунктах</small>
            </div>
            <div className="factor-list">
              {factors.map((factor) => (
                <div className="factor-row" key={factor.label}>
                  <div><span>{factor.label}<small>{Math.round(factor.weight * 100)}%</small></span><strong className={factor.contribution >= 0 ? "factor--positive" : "factor--negative"}>{formatScore(factor.contribution)} п.</strong></div>
                  <i><b className={factor.contribution >= 0 ? "factor--positive" : "factor--negative"} style={{ width: `${Math.min(Math.abs(factor.contribution) * 3, 100)}%` }} /></i>
                </div>
              ))}
            </div>
            <div className="factor-total"><span>Сумма вкладов</span><strong>{formatScore(signal.score)} п.</strong></div>
            <button className="method-link" type="button" onClick={onMethodology}><BookOpen size={13} /> Как считается сигнал <ArrowUpRight size={11} /></button>
          </section>
        </div>

        <section className="news-card">
          <div className="section-heading">
            <span><Newspaper size={15} /> Новости, изменившие сигнал</span>
            <button type="button" onClick={onOpenNews}>Все новости {signal.ticker} <ArrowUpRight size={11} /></button>
          </div>
          <div className="news-list">
            {signal.evidence.map((item, index) => (
              <button className="news-item news-item--button" type="button" key={`${item.source}-${item.time}-${index}`} onClick={() => onReadNews({ ...item, signal, index })}>
                <span className="news-index">0{index + 1}</span>
                <div className="news-content">
                  <div><span>{item.source}</span><i>{item.tag}</i></div>
                  <h2>{item.title}</h2>
                  <p>{signal.summary}</p>
                </div>
                <span className="news-time"><Clock3 size={12} /> {item.time}</span>
                <span className="news-open"><BookOpen size={14} /></span>
              </button>
            ))}
          </div>
        </section>

        <p className="disclaimer">Сигнал является аналитической гипотезой и не является индивидуальной инвестиционной рекомендацией.</p>
      </section>
    </main>
  );
}

function NewsScreen({ initialTicker, onReadNews }) {
  const [ticker, setTicker] = useState(initialTicker || "all");
  const [query, setQuery] = useState("");

  useEffect(() => setTicker(initialTicker || "all"), [initialTicker]);

  const items = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase("ru-RU");
    return allNews.filter((item) => {
      const matchesTicker = ticker === "all" || item.signal.ticker === ticker;
      const haystack = `${item.title} ${item.source} ${item.signal.ticker} ${item.signal.company}`.toLocaleLowerCase("ru-RU");
      return matchesTicker && (!normalized || haystack.includes(normalized));
    });
  }, [ticker, query]);

  return (
    <main className="screen section-screen">
      <section className="page-hero">
        <div><span className="eyebrow"><Newspaper size={13} /> Лента событий</span><h1>Новости, которые двигают сигнал</h1><p>Открой публикацию, прочитай краткое содержание и сразу увидь, с какой компанией и сигналом она связана.</p></div>
        <label className="page-search"><Search size={15} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Компания, тикер или событие" />{query && <button type="button" onClick={() => setQuery("")} aria-label="Очистить поиск"><X size={13} /></button>}</label>
      </section>
      <div className="news-filters" aria-label="Фильтр по компании">
        <button type="button" className={ticker === "all" ? "is-active" : ""} onClick={() => setTicker("all")}>Все <span>{allNews.length}</span></button>
        {signals.map((signal) => (
          <button type="button" key={signal.ticker} className={ticker === signal.ticker ? "is-active" : ""} onClick={() => setTicker(signal.ticker)}><CompanyMark signal={signal} small />{signal.ticker}</button>
        ))}
      </div>
      <section className="news-feed">
        <div className="feed-heading"><span>{items.length} публикаций</span><small>демонстрационные материалы</small></div>
        {items.map((item) => (
          <button type="button" className="feed-item" key={item.id} onClick={() => onReadNews(item)}>
            <CompanyMark signal={item.signal} />
            <div className="feed-copy"><div><span>{item.source}</span><i>{item.tag}</i><time>{item.time}</time></div><h2>{item.title}</h2><p>{item.signal.summary}</p><footer><strong>{item.signal.ticker}</strong><Direction direction={item.signal.direction} /><span>{formatScore(item.signal.score)} п.</span></footer></div>
            <BookOpen size={17} />
          </button>
        ))}
        {!items.length && <div className="empty-state"><Search size={22} /><strong>Новостей не найдено</strong><span>Измени запрос или выбери другую компанию.</span></div>}
      </section>
    </main>
  );
}

function MethodologyScreen({ onApi }) {
  const sample = signals[0];
  const sampleFactors = scoreFactors(sample.score);

  return (
    <main className="screen section-screen methodology-screen">
      <section className="page-hero methodology-hero">
        <div><span className="eyebrow"><BookOpen size={13} /> Прозрачная методика</span><h1>Как считается сигнал</h1><p>LLM не предсказывает цену напрямую. Она извлекает из новости факты и смысл, после чего детерминированная формула собирает итоговую оценку.</p></div>
        <div className="method-score-scale"><span>Вниз</span><i><b /></i><span>Нейтрально</span><i><b /></i><span>Вверх</span><small>−100</small><small>−18</small><small>+18</small><small>+100</small></div>
      </section>

      <section className="score-definition">
        <Info size={18} />
        <div><h2>Что такое пункты оценки</h2><p><strong>{formatScore(sample.score)} п.</strong> — не «акция вырастет на 42,7%». Это нормализованная сила аналитической гипотезы на шкале от −100 до +100. Чем дальше значение от нуля, тем сильнее направленный сигнал.</p></div>
      </section>

      <div className="method-grid">
        <section className="method-card">
          <header><span>01</span><div><h2>LLM разбирает новость</h2><p>Возвращает структурированные признаки, а не готовый торговый совет.</p></div></header>
          <div className="method-factors">
            {scoreFactorDefinitions.map((factor) => <div key={factor.label}><span>{factor.label}</span><p>{factor.description}</p><strong>{Math.round(factor.weight * 100)}%</strong></div>)}
          </div>
        </section>
        <section className="method-card formula-card">
          <header><span>02</span><div><h2>Формула складывает вклад</h2><p>Веса зафиксированы в версии модели и проверяются тестами.</p></div></header>
          <div className="formula-line"><code>score = Σ (признак × вес)</code><span>ограничение: −100…+100</span></div>
          <div className="sample-calculation">
            <div className="sample-company"><CompanyMark signal={sample} /><span><strong>{sample.ticker}</strong><small>пример расчёта</small></span></div>
            {sampleFactors.map((factor) => <div key={factor.label}><span>{factor.label}</span><strong>{formatScore(factor.contribution)} п.</strong></div>)}
            <footer><span>Итог</span><strong>{formatScore(sample.score)} п.</strong></footer>
          </div>
        </section>
        <section className="method-card threshold-card">
          <header><span>03</span><div><h2>Порог превращает оценку в действие</h2><p>Нейтральная зона защищает от решений на слабом информационном шуме.</p></div></header>
          <div><span className="direction direction--down"><ArrowDownRight size={14} /> Вниз</span><strong>≤ −18</strong><p>Сократить риск или не входить</p></div>
          <div><span className="direction direction--neutral"><Minus size={14} /> Нейтрально</span><strong>от −18 до +18</strong><p>Ждать нового факта</p></div>
          <div><span className="direction direction--up"><ArrowUpRight size={14} /> Вверх</span><strong>≥ +18</strong><p>Рассмотреть позицию</p></div>
        </section>
      </div>

      <section className="method-reality">
        <div><ShieldCheck size={17} /><span><strong>Что работает сейчас</strong>Новостной baseline: семантические признаки, фиксированные веса и версионируемый расчёт.</span></div>
        <div><Database size={17} /><span><strong>Следующие улучшения</strong>Фундаментальные показатели, цена и объём будут добавляться как отдельные проверяемые блоки — без скрытой магии.</span></div>
        <button type="button" onClick={onApi}>Посмотреть API <ArrowUpRight size={13} /></button>
      </section>
    </main>
  );
}

const apiEndpoints = [
  { id: "signals", method: "GET", path: "/v1/signals?limit=20", title: "Список сигналов", description: "Последние рассчитанные сигналы с фильтрами по тикеру и направлению." },
  { id: "ticker", method: "GET", path: "/v1/signals?ticker=SBER&limit=1", title: "Сигнал компании", description: "Последний доступный сигнал по выбранному тикеру." },
  { id: "health", method: "GET", path: "/health/ready", title: "Готовность сервиса", description: "Проверка приложения и соединения с хранилищем." },
];

function ApiScreen() {
  const [selectedId, setSelectedId] = useState("signals");
  const [status, setStatus] = useState("idle");
  const [copied, setCopied] = useState("");
  const endpoint = apiEndpoints.find((item) => item.id === selectedId);
  const baseUrl = typeof window === "undefined" ? "" : window.location.origin;

  const checkApi = async () => {
    setStatus("checking");
    try {
      const response = await fetch("/health/live", { headers: { Accept: "application/json" } });
      setStatus(response.ok ? "online" : "offline");
    } catch {
      setStatus("offline");
    }
  };

  const copyText = async (value, id) => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(id);
      window.setTimeout(() => setCopied(""), 1600);
    } catch {
      setCopied("");
    }
  };

  return (
    <main className="screen section-screen api-screen">
      <section className="page-hero api-hero">
        <div><span className="eyebrow"><Braces size={13} /> EventEdge API</span><h1>Получай сигналы через простой HTTP API</h1><p>Интерфейс для внутренних продуктов, аналитических пайплайнов и автоматизации. Сейчас доступно чтение рассчитанных сигналов и health-check.</p></div>
        <div className={`api-status api-status--${status}`}><Server size={17} /><span><strong>{status === "checking" ? "Проверяем…" : status === "online" ? "API отвечает" : status === "offline" ? "API недоступен" : "Статус не проверен"}</strong><small>{baseUrl}</small></span><button type="button" onClick={checkApi} disabled={status === "checking"}><RefreshCw size={14} /> Проверить</button></div>
      </section>

      <section className="api-base-url"><div><span>Base URL</span><code>{baseUrl}</code></div><button type="button" onClick={() => copyText(baseUrl, "base")}><Copy size={14} /> {copied === "base" ? "Скопировано" : "Скопировать"}</button></section>

      <div className="api-layout">
        <aside className="endpoint-list" aria-label="Методы API">
          <span>Методы</span>
          {apiEndpoints.map((item) => <button type="button" key={item.id} className={selectedId === item.id ? "is-active" : ""} onClick={() => setSelectedId(item.id)}><b>{item.method}</b><span>{item.title}<small>{item.path.split("?")[0]}</small></span></button>)}
        </aside>
        <section className="endpoint-doc">
          <header><div><span className="http-method">{endpoint.method}</span><code>{endpoint.path}</code></div><button type="button" onClick={() => copyText(`${baseUrl}${endpoint.path}`, endpoint.id)}><Copy size={14} /> {copied === endpoint.id ? "Скопировано" : "Копировать URL"}</button></header>
          <h2>{endpoint.title}</h2><p>{endpoint.description}</p>
          {endpoint.id !== "health" && <div className="parameter-table"><div><strong>Параметр</strong><strong>Тип</strong><strong>Описание</strong></div><div><code>{endpoint.id === "ticker" ? "ticker" : "limit"}</code><span>{endpoint.id === "ticker" ? "string" : "integer"}</span><p>{endpoint.id === "ticker" ? "Тикер MOEX, например SBER" : "Количество записей, максимум 100"}</p></div></div>}
          <div className="code-panel"><div><span><Terminal size={13} /> cURL</span><button type="button" onClick={() => copyText(`curl -s '${baseUrl}${endpoint.path}'`, "curl")}><Copy size={13} /> {copied === "curl" ? "Готово" : "Копировать"}</button></div><pre><code>{`curl -s '${baseUrl}${endpoint.path}' \\\n  -H 'Accept: application/json'`}</code></pre></div>
          <div className="response-panel"><span>Пример ответа</span><pre><code>{endpoint.id === "health" ? `{"status":"ready"}` : `{
  "data": [
    {
      "id": "sig_01JZK6K5GDX90Q2X8C0R4D7M9P",
      "ticker": "SBER",
      "direction": "up",
      "score": 42.7,
      "confidence": 0.76,
      "horizon": {"value": 3, "unit": "trading_days"},
      "model_version": "news-baseline-0.1.0"
    }
  ],
  "meta": {"limit": 20, "has_more": false, "next_cursor": null}
}`}</code></pre></div>
        </section>
      </div>
    </main>
  );
}

function NewsReader({ item, onClose }) {
  if (!item) return null;
  const { signal } = item;
  return (
    <div className="reader-overlay">
      <button className="overlay-dismiss" type="button" aria-label="Закрыть новость" onClick={onClose} />
      <article className="reader-dialog" role="dialog" aria-modal="true" aria-label="Просмотр новости">
        <header><div><CompanyMark signal={signal} /><span><strong>{signal.ticker}</strong><small>{signal.company} · {item.source}</small></span></div><button type="button" onClick={onClose} aria-label="Закрыть"><X size={18} /></button></header>
        <div className="reader-meta"><span>{item.tag}</span><time><Clock3 size={12} /> сегодня, {item.time}</time></div>
        <h1>{item.title}</h1>
        <div className="reader-body"><p>{signal.summary}</p><p>EventEdge связал публикацию с событием «{signal.event}», проверил её новизну, существенность и качество источника. На горизонте {signal.horizon} текущая оценка составляет <strong>{formatScore(signal.score)} пункта</strong>.</p></div>
        <section className="reader-insight"><CircleGauge size={16} /><div><span>Что это меняет</span><strong>{signal.action}</strong><p>{signal.invalidation}</p></div></section>
        <footer><FileText size={13} /> Это краткое содержание EventEdge для демонстрационного материала, а не полный текст исходной публикации.</footer>
      </article>
    </div>
  );
}

function parseRoute() {
  const value = window.location.hash.replace(/^#\/?/, "") || "signals";
  const [view, ticker] = value.split("/");
  return { view: ["signals", "signal", "news", "methodology", "api"].includes(view) ? view : "signals", ticker: ticker || null };
}

export default function App() {
  const [route, setRoute] = useState(parseRoute);
  const [readerItem, setReaderItem] = useState(null);

  useEffect(() => {
    const handleHashChange = () => setRoute(parseRoute());
    window.addEventListener("hashchange", handleHashChange);
    return () => window.removeEventListener("hashchange", handleHashChange);
  }, []);

  const navigate = (view, ticker = null) => {
    const nextHash = `#${view}${ticker ? `/${ticker}` : ""}`;
    if (window.location.hash === nextHash) setRoute({ view, ticker });
    else window.location.hash = nextHash;
    window.scrollTo({ top: 0 });
  };

  const selectedSignal = signals.find((signal) => signal.ticker === route.ticker) || signals[0];

  return (
    <div className="app-shell">
      <AppHeader view={route.view} onNavigate={navigate} onSelect={(ticker) => navigate("signal", ticker)} />
      {route.view === "signals" && <SignalsScreen onSelect={(ticker) => navigate("signal", ticker)} onMethodology={() => navigate("methodology")} />}
      {route.view === "signal" && <CompanyScreen signal={selectedSignal} onBack={() => navigate("signals")} onMethodology={() => navigate("methodology")} onOpenNews={() => navigate("news", selectedSignal.ticker)} onReadNews={setReaderItem} />}
      {route.view === "news" && <NewsScreen initialTicker={route.ticker} onReadNews={setReaderItem} />}
      {route.view === "methodology" && <MethodologyScreen onApi={() => navigate("api")} />}
      {route.view === "api" && <ApiScreen />}
      <NewsReader item={readerItem} onClose={() => setReaderItem(null)} />
    </div>
  );
}
