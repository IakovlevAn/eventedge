import {
  ArrowDownRight,
  ArrowLeft,
  ArrowUpRight,
  Activity,
  BarChart3,
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
  WalletCards,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";

const API_BASE = (import.meta.env.VITE_API_BASE || "").replace(/\/$/, "");
const apiUrl = (path) => `${API_BASE}${path}`;

const methodologySignals = [
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
  SBER: { colors: ["#21a366", "#0c7650"], glyph: "СБ", asset: "/brands/SBER.png" },
  LKOH: { colors: ["#ee2d33", "#9c101d"], glyph: "ЛК", asset: "/brands/LKOH.png" },
  YDEX: { colors: ["#ffcc00", "#f04b3f"], glyph: "Я", asset: "/brands/YDEX.png" },
  NVTK: { colors: ["#2863b3", "#173f7d"], glyph: "НТ", asset: "/brands/NVTK.png" },
  TATN: { colors: ["#008d6b", "#006047"], glyph: "ТН", asset: "/brands/TATN.png" },
  ROSN: { colors: ["#f2c500", "#111216"], glyph: "РН", asset: "/brands/ROSN.png" },
  GMKN: { colors: ["#1f7ca8", "#124b72"], glyph: "НН", asset: "/brands/GMKN.png" },
  MGNT: { colors: ["#ef3340", "#a71930"], glyph: "МГ", asset: "/brands/MGNT.png" },
  SIBN: { colors: ["#1686c8", "#075487"], glyph: "ГН", asset: "/brands/SIBN.png" },
  GAZP: { colors: ["#1686c8", "#075487"], glyph: "ГП", asset: "/brands/GAZP.png" },
  VTBR: { colors: ["#1783ca", "#174687"], glyph: "ВТ", asset: "/brands/VTBR.png" },
  PLZL: { colors: ["#d9aa36", "#8d6712"], glyph: "ПЛ", asset: "/brands/PLZL.png" },
  CHMF: { colors: ["#586474", "#151a21"], glyph: "СВ", asset: "/brands/CHMF.png" },
  ALRS: { colors: ["#45a4b3", "#236a77"], glyph: "АЛ", asset: "/brands/ALRS.png" },
  MOEX: { colors: ["#174d86", "#112f53"], glyph: "МБ", asset: "/brands/MOEX.png" },
};

const companyMeta = {
  SBER: ["Сбербанк", "Финансы"], LKOH: ["Лукойл", "Нефть и газ"],
  YDEX: ["Яндекс", "Технологии"], NVTK: ["Новатэк", "Нефть и газ"],
  TATN: ["Татнефть", "Нефть и газ"], ROSN: ["Роснефть", "Нефть и газ"],
  GMKN: ["Норникель", "Металлы"], MGNT: ["Магнит", "Ритейл"],
  SIBN: ["Газпром нефть", "Нефть и газ"],
  GAZP: ["Газпром", "Нефть и газ"], VTBR: ["ВТБ", "Финансы"],
  PLZL: ["Полюс", "Металлы"], CHMF: ["Северсталь", "Металлы"],
  ALRS: ["АЛРОСА", "Металлы"], MOEX: ["Московская биржа", "Финансы"],
};

const actionLabels = {
  consider_buy: "Рассмотреть покупку",
  no_action: "Ничего не делать",
  review_position: "Сократить или защитить позицию",
};

const sourceLabels = {
  moex_news: "Московская биржа",
  cbr_press: "Банк России",
  interfax: "Интерфакс",
  tass: "ТАСС",
  rbc: "РБК",
  google_news: "Новостная подборка",
  market_background: "Рыночный фон",
};

const methodologySources = [
  { id: "cbr_press", name: "Банк России", kind: "Первичный", quality: 95, freshness: "до 15 мин", role: "Макроэкономические решения и пресс-релизы регулятора", url: "https://www.cbr.ru/press/" },
  { id: "moex_news", name: "Московская биржа", kind: "Первичный", quality: 95, freshness: "цель ≤ 2 мин", role: "Сообщения биржи и эмитентов", url: "https://www.moex.com/ru/news/" },
  { id: "interfax", name: "Интерфакс", kind: "Агентство", quality: 90, freshness: "цель ≤ 2 мин", role: "Оперативные корпоративные и рыночные новости", url: "https://www.interfax.ru/business/" },
  { id: "tass", name: "ТАСС", kind: "Агентство", quality: 82, freshness: "цель ≤ 2 мин", role: "Подтверждение значимых событий", url: "https://tass.ru/ekonomika" },
  { id: "rbc", name: "РБК", kind: "Медиа", quality: 78, freshness: "цель ≤ 2 мин", role: "Рыночный контекст и дополнительное подтверждение", url: "https://www.rbc.ru/quote/" },
  { id: "google_news", name: "Google News", kind: "Discovery", quality: 74, freshness: "до 5 мин", role: "Поиск публикаций; не считается первичным источником", url: "https://news.google.com/" },
  { id: "market_background", name: "Рыночный фон", kind: "Контекст", quality: 74, freshness: "до 5 мин", role: "Ставка, рубль, нефть, санкции и общий фон рынка", url: "https://news.google.com/" },
  { id: "moex_iss", name: "MOEX ISS", kind: "Рыночные данные", quality: 100, freshness: "до 60 сек", role: "Цена, объём, свечи, ликвидность и волатильность", url: "https://iss.moex.com/iss/" },
];

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

function formatPrice(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(Number(value))} ₽`;
}

function formatPct(value, { sign = true } = {}) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  const number = Number(value);
  return `${sign && number > 0 ? "+" : ""}${new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 1, maximumFractionDigits: 2 }).format(number)}%`;
}

function formatCompact(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return new Intl.NumberFormat("ru-RU", { notation: "compact", maximumFractionDigits: 1 }).format(Number(value));
}

function formatCurrency(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 }).format(Number(value))} ₽`;
}

function formatScenario(scenario) {
  if (!scenario) return "Недоступен";
  return `${formatPct(scenario.low_pct)} … ${formatPct(scenario.high_pct)}`;
}

function formatRelative(timestamp) {
  const elapsed = Math.max(0, Date.now() - new Date(timestamp).getTime());
  const minutes = Math.floor(elapsed / 60000);
  if (minutes < 1) return "только что";
  if (minutes < 60) return `${minutes} мин`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} ч`;
  return new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short" }).format(new Date(timestamp));
}

function formatTime(timestamp) {
  return new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" }).format(new Date(timestamp));
}

function horizonLabel(horizon) {
  const units = horizon.unit === "calendar_days" ? "дн." : horizon.unit === "trading_days" ? "торг. дн." : "ч";
  return `${horizon.value} ${units}`;
}

function signalFromApi(item) {
  const [company, sector] = companyMeta[item.ticker] || [item.ticker, "Российский рынок"];
  return {
    ...item,
    company,
    sector,
    confidence: Math.round(item.confidence * 100),
    horizon: horizonLabel(item.horizon),
    event: "Новостное событие",
    price: "—",
    change: "—",
    market: null,
    scenario: null,
    updated: formatRelative(item.as_of),
    action: actionLabels[item.action] || item.action,
    invalidation: (item.invalidation_conditions || []).join(" "),
    factors: (item.factor_contributions || []).map((factor) => ({
      label: factor.label,
      contribution: Number((factor.contribution * 100).toFixed(1)),
    })),
    evidence: [],
  };
}

function assessmentFromApi(item) {
  const [company, sector] = companyMeta[item.ticker] || [item.ticker, "Российский рынок"];
  return {
    ...item,
    displayDirection: item.bias_direction || item.direction,
    company,
    sector,
    confidence: Math.round(item.confidence * 100),
    horizon: horizonLabel(item.horizon),
    event: item.assessment_type === "hybrid" ? "Новость + рынок" : "Рыночная оценка",
    price: formatPrice(item.market?.last_price),
    change: formatPct(item.market?.daily_change_pct),
    market: item.market || null,
    scenario: item.scenario || null,
    series: item.market?.candles || [],
    updated: formatRelative(item.as_of),
    action: actionLabels[item.action] || item.action,
    invalidation: item.assessment_type === "hybrid"
      ? "Пересмотреть оценку при новой существенной новости или смене реакции рынка."
      : "Рыночная оценка обновляется вместе с ценой и не заменяет новостной сигнал.",
    factors: (item.factor_contributions || []).map((factor) => ({
      ...factor,
      label: factor.label,
      contribution: Number(Number(factor.contribution || 0).toFixed(1)),
    })),
    evidence: [],
  };
}

function newsFromApi(item, signalsById) {
  const related = item.related_signals?.[0];
  const tickers = [...new Set([
    ...(item.source_metadata?.tickers || []),
    ...(related?.ticker ? [related.ticker] : []),
  ])];
  const [company, sector] = related ? companyMeta[related.ticker] || [related.ticker, "Российский рынок"] : [];
  const signal = related ? signalsById.get(related.id) || {
    ...related,
    company,
    sector,
    confidence: Math.round(related.confidence * 100),
    horizon: "3 дн.",
    action: actionLabels[related.action] || related.action,
    summary: "Ретроспективная оценка эффекта новости на момент её публикации.",
    evidence: [],
  } : null;
  return {
    id: item.id,
    source: sourceLabels[item.source_id] || item.source_id,
    sourceId: item.source_id,
    time: formatTime(item.published_at),
    publishedAt: item.published_at,
    processedAt: item.created_at,
    tag: signal ? related.status === "active" ? "Активный сигнал" : "Исторический сигнал" : "Без сигнала",
    title: item.title,
    content: item.content,
    url: item.url,
    signal,
    tickers,
    companySignal: !signal && tickers[0] ? {
      ticker: tickers[0],
      company: companyMeta[tickers[0]]?.[0] || tickers[0],
      sector: companyMeta[tickers[0]]?.[1] || "Российский рынок",
    } : null,
  };
}

const titleStopWords = new Set([
  "для", "как", "при", "что", "это", "его", "она", "они", "или", "уже", "будет", "после", "перед", "под", "над", "без", "из", "на", "по", "в", "во", "и", "а", "но", "к", "ко", "с", "со", "от", "до", "о", "об", "за", "не",
]);

function titleTokens(title) {
  return new Set(
    title
      .toLocaleLowerCase("ru-RU")
      .replace(/[^a-zа-яё0-9]+/giu, " ")
      .split(/\s+/)
      .filter((token) => token.length > 2 && !titleStopWords.has(token)),
  );
}

function sameNewsEvent(left, right) {
  const leftTicker = left.signal?.ticker || left.tickers?.[0];
  const rightTicker = right.signal?.ticker || right.tickers?.[0];
  if (leftTicker && rightTicker && leftTicker !== rightTicker) return false;
  if (left.signal && right.signal && left.signal.direction !== right.signal.direction) return false;
  if (Math.abs(new Date(left.publishedAt) - new Date(right.publishedAt)) > 48 * 60 * 60 * 1000) return false;
  const leftTokens = titleTokens(left.title);
  const rightTokens = titleTokens(right.title);
  if (!leftTokens.size || !rightTokens.size) return false;
  const overlap = [...leftTokens].filter((token) => rightTokens.has(token)).length;
  return overlap / Math.min(leftTokens.size, rightTokens.size) >= 0.6;
}

function groupNewsEvents(items) {
  const groups = [];
  items.forEach((item) => {
    const group = groups.find((candidate) => sameNewsEvent(candidate, item));
    if (!group) {
      groups.push({
        ...item,
        sourceCount: 1,
        sources: [{ id: item.sourceId, name: item.source, url: item.url, publishedAt: item.publishedAt }],
        corroborations: [],
      });
      return;
    }
    if (!group.signal && item.signal) group.signal = item.signal;
    group.tickers = [...new Set([...(group.tickers || []), ...(item.tickers || [])])];
    group.corroborations.push(item);
    if (!group.sources.some((source) => source.name === item.source && source.url === item.url)) {
      group.sources.push({ id: item.sourceId, name: item.source, url: item.url, publishedAt: item.publishedAt });
      group.sourceCount = group.sources.length;
    }
  });
  return groups;
}

function CompanyMark({ signal, small = false }) {
  const [imageFailed, setImageFailed] = useState(false);
  const brand = companyBrands[signal.ticker] || { colors: ["#717785", "#353945"], glyph: signal.ticker.slice(0, 1), asset: null };
  return (
    <span
      className={`company-mark ${small ? "company-mark--small" : ""}`}
      style={{ "--company-color": brand.colors[0], "--company-color-deep": brand.colors[1] }}
      data-ticker={signal.ticker}
    >
      {brand.asset && !imageFailed && <img src={brand.asset} alt="" loading="lazy" onError={() => setImageFailed(true)} />}
      {(!brand.asset || imageFailed) && <b aria-hidden="true">{brand.glyph}</b>}
    </span>
  );
}

function Direction({ direction, label = null }) {
  const meta = directionMeta[direction];
  const Icon = meta.Icon;
  return (
    <span className={`direction direction--${direction}`}>
      <Icon size={13} strokeWidth={2.2} />
      {label || meta.label}
    </span>
  );
}

function EventBubbles({ events = [], onOpen, compact = false }) {
  if (!events.length) return <span className="event-bubbles__empty">События ещё не привязаны</span>;
  return (
    <div className={`event-bubbles ${compact ? "event-bubbles--compact" : ""}`} aria-label="События, повлиявшие на сигнал">
      {events.slice(0, compact ? 5 : 8).map((item, index) => {
        const direction = item.signal?.direction || "neutral";
        const impact = Math.min(Math.abs(item.signal?.score || 0), 100);
        const size = (compact ? 13 : 18) + Math.round(impact * (compact ? 0.09 : 0.14)) + Math.min((item.sourceCount || 1) - 1, 3) * 2;
        const label = `${item.title}. ${item.sourceCount || 1} ${item.sourceCount === 1 ? "источник" : "источника"}`;
        return (
          <button
            type="button"
            className={`event-bubble event-bubble--${direction}`}
            style={{ "--event-size": `${size}px`, "--event-order": index }}
            key={item.id}
            onClick={(event) => { event.stopPropagation(); onOpen?.(item); }}
            aria-label={label}
            title={label}
          >
            <span>{item.sourceCount > 1 ? item.sourceCount : ""}</span>
          </button>
        );
      })}
    </div>
  );
}

function PriceChart({ dailySeries = [], intradaySeries = [], intradayStatus = "idle", direction = "neutral", events = [], onOpen }) {
  const [range, setRange] = useState("5d");
  const [mode, setMode] = useState("candles");
  const [hoveredIndex, setHoveredIndex] = useState(null);
  const [selectedClusterKey, setSelectedClusterKey] = useState(null);
  const [selectedEventId, setSelectedEventId] = useState(null);

  const normalizedDaily = dailySeries.map((item, index) => typeof item === "number" ? {
    begin: new Date(Date.now() - (dailySeries.length - index) * 86400000).toISOString(),
    open: item,
    close: item,
    high: item,
    low: item,
    volume_shares: 0,
  } : item);
  const isIntraday = range === "1d" || range === "5d";
  const moscowDay = (timestamp) => new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Moscow", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(timestamp));
  const intradayDays = [...new Set(intradaySeries.map((candle) => moscowDay(candle.begin)))];
  const visibleDays = new Set(intradayDays.slice(-(range === "1d" ? 1 : 5)));
  const intradayCandles = intradaySeries.filter((candle) => visibleDays.has(moscowDay(candle.begin)));
  const allCandles = isIntraday ? intradayCandles : normalizedDaily;
  const candles = isIntraday ? allCandles : allCandles.slice(-(range === "3m" ? 66 : 22));

  if (candles.length < 2) return <div className="price-chart__empty">{isIntraday && intradayStatus === "loading" ? "Загружаем 10-минутные свечи MOEX…" : "Недостаточно свечей MOEX"}</div>;

  const width = 860;
  const height = 318;
  const plotTop = 16;
  const plotBottom = 222;
  const volumeTop = 246;
  const volumeBottom = 286;
  const labelRight = 68;
  const plotWidth = width - labelRight;
  const lows = candles.map((candle) => Number(candle.low ?? candle.close));
  const highs = candles.map((candle) => Number(candle.high ?? candle.close));
  const min = Math.min(...lows);
  const max = Math.max(...highs);
  const spread = Math.max(max - min, 0.01);
  const volumes = candles.map((candle) => Number(candle.volume_shares || 0));
  const maxVolume = Math.max(...volumes, 1);
  const xForIndex = (index) => (index / (candles.length - 1)) * plotWidth;
  const yForPrice = (value) => plotBottom - ((Number(value) - min) / spread) * (plotBottom - plotTop);
  const points = candles.map((candle, index) => `${xForIndex(index).toFixed(1)},${yForPrice(candle.close).toFixed(1)}`).join(" ");
  const startTime = new Date(candles[0].begin).getTime();
  const endTime = new Date(candles[candles.length - 1].begin).getTime();
  const eventImpact = (event) => event.signal ? Math.min(Math.abs(event.signal.score || 0), 100) : Math.min(18 + (event.sourceCount || 1) * 8, 42);
  const nearestCandleIndex = (timestamp) => candles.reduce((bestIndex, candle, index) => (
    Math.abs(new Date(candle.begin).getTime() - timestamp) < Math.abs(new Date(candles[bestIndex].begin).getTime() - timestamp) ? index : bestIndex
  ), 0);
  const groupedEvents = events
    .filter((event) => {
      const publishedAt = new Date(event.publishedAt).getTime();
      const endPadding = isIntraday ? 10 * 60 * 1000 : 86400000;
      return event.publishedAt && publishedAt >= startTime && publishedAt <= endTime + endPadding;
    })
    .reduce((groups, event) => {
      const candleIndex = nearestCandleIndex(new Date(event.publishedAt).getTime());
      const key = `${candles[candleIndex].begin}:${candleIndex}`;
      const group = groups.get(key) || { key, candleIndex, events: [] };
      group.events.push(event);
      groups.set(key, group);
      return groups;
    }, new Map());
  const eventClusters = [...groupedEvents.values()]
    .map((cluster) => {
      const sortedEvents = cluster.events.sort((left, right) => eventImpact(right) - eventImpact(left) || new Date(right.publishedAt) - new Date(left.publishedAt));
      const strongest = sortedEvents[0];
      const impact = eventImpact(strongest);
      const newsCount = sortedEvents.reduce((sum, event) => sum + (event.sourceCount || 1), 0);
      return {
        ...cluster,
        events: sortedEvents,
        strongest,
        impact,
        newsCount,
        x: xForIndex(cluster.candleIndex),
        y: Math.max(plotTop + 15, yForPrice(candles[cluster.candleIndex].high) - 21),
        radius: 7 + impact * 0.055 + Math.min(sortedEvents.length - 1, 3),
        direction: strongest.signal?.direction || "neutral",
      };
    })
    .sort((left, right) => left.candleIndex - right.candleIndex);
  const tickIndexes = [...new Set([0, Math.floor((candles.length - 1) * 0.25), Math.floor((candles.length - 1) * 0.5), Math.floor((candles.length - 1) * 0.75), candles.length - 1])];
  const activeIndex = hoveredIndex ?? candles.length - 1;
  const activeCandle = candles[activeIndex];
  const previousClose = candles[Math.max(0, activeIndex - 1)]?.close;
  const activeChange = previousClose ? ((Number(activeCandle.close) / Number(previousClose)) - 1) * 100 : 0;
  const periodChange = ((Number(candles[candles.length - 1].close) / Number(candles[0].close)) - 1) * 100;
  const strongestCluster = [...eventClusters].sort((left, right) => right.impact - left.impact)[0];
  const selectedCluster = eventClusters.find((cluster) => cluster.key === selectedClusterKey) || strongestCluster;
  const selectedEvent = selectedCluster?.events.find((event) => event.id === selectedEventId) || selectedCluster?.events[0];

  const handlePointerMove = (event) => {
    const svg = event.currentTarget;
    const matrix = svg.getScreenCTM();
    if (!matrix) return;
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    const local = point.matrixTransform(matrix.inverse());
    const ratio = Math.min(1, Math.max(0, local.x / plotWidth));
    setHoveredIndex(Math.round(ratio * (candles.length - 1)));
  };
  const moveCursor = (step) => {
    setHoveredIndex((current) => Math.min(candles.length - 1, Math.max(0, (current ?? candles.length - 1) + step)));
  };

  return (
    <div className="price-chart-shell">
      <div className="chart-toolbar">
        <div><strong>История цены</strong><span>{isIntraday ? "10-минутные свечи · MOEX" : "Дневные свечи · MOEX"}</span></div>
        <div className="chart-toolbar__controls">
          <div className="chart-segment" role="group" aria-label="Вид графика">
            <button type="button" className={mode === "line" ? "is-active" : ""} aria-pressed={mode === "line"} onClick={() => setMode("line")}>Линия</button>
            <button type="button" className={mode === "candles" ? "is-active" : ""} aria-pressed={mode === "candles"} onClick={() => setMode("candles")}>Свечи</button>
          </div>
          <div className="chart-segment" role="group" aria-label="Период графика">
            <button type="button" className={range === "1d" ? "is-active" : ""} aria-pressed={range === "1d"} onClick={() => { setRange("1d"); setHoveredIndex(null); }}>1Д</button>
            <button type="button" className={range === "5d" ? "is-active" : ""} aria-pressed={range === "5d"} onClick={() => { setRange("5d"); setHoveredIndex(null); }}>5Д</button>
            <button type="button" className={range === "1m" ? "is-active" : ""} aria-pressed={range === "1m"} onClick={() => { setRange("1m"); setHoveredIndex(null); }}>1М</button>
            <button type="button" className={range === "3m" ? "is-active" : ""} aria-pressed={range === "3m"} disabled={normalizedDaily.length < 30} onClick={() => { setRange("3m"); setHoveredIndex(null); }}>3М</button>
          </div>
        </div>
      </div>
      <div className="chart-readout" aria-live="polite">
        <div><span>{hoveredIndex === null ? "Последняя свеча" : "Свеча"}</span><strong>{new Intl.DateTimeFormat("ru-RU", isIntraday ? { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" } : { day: "numeric", month: "long", year: "numeric" }).format(new Date(activeCandle.begin))}</strong></div>
        <span>О <b>{formatPrice(activeCandle.open)}</b></span>
        <span>МАКС <b>{formatPrice(activeCandle.high)}</b></span>
        <span>МИН <b>{formatPrice(activeCandle.low)}</b></span>
        <span>ЗАКР <b>{formatPrice(activeCandle.close)}</b></span>
        <span className={activeChange >= 0 ? "market-positive" : "market-negative"}>{formatPct(activeChange)}</span>
        <span>ОБЪЁМ <b>{formatCompact(activeCandle.volume_shares)}</b></span>
      </div>
      <div className="chart-visual" tabIndex="0" onKeyDown={(event) => { if (event.key === "ArrowLeft") moveCursor(-1); if (event.key === "ArrowRight") moveCursor(1); }} aria-label="График цены. Стрелки влево и вправо меняют выбранную торговую сессию.">
        <svg className={`price-chart price-chart--${direction}`} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`Цена, объём и события по ${isIntraday ? "10-минутным" : "дневным"} свечам MOEX`} onPointerMove={handlePointerMove} onPointerLeave={() => setHoveredIndex(null)}>
          {[0, 0.25, 0.5, 0.75, 1].map((ratio) => {
            const y = plotTop + ratio * (plotBottom - plotTop);
            const price = max - ratio * spread;
            return <g key={ratio}><line className="chart-grid" x1="0" y1={y} x2={plotWidth} y2={y} /><text className="chart-axis-label" x={plotWidth + 10} y={y + 4}>{new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(price)}</text></g>;
          })}
          <polygon className="chart-area" points={`0,${plotBottom} ${points} ${plotWidth},${plotBottom}`} />
          {mode === "line" && <polyline className="chart-price-line" points={points} />}
          {mode === "candles" && candles.map((candle, index) => {
            const x = xForIndex(index);
            const bodyWidth = Math.max(3, Math.min(9, plotWidth / candles.length - 2));
            const openY = yForPrice(candle.open);
            const closeY = yForPrice(candle.close);
            const rising = Number(candle.close) >= Number(candle.open);
            return <g className={`chart-candle chart-candle--${rising ? "up" : "down"}`} key={`candle-${candle.begin || index}`}><line x1={x} y1={yForPrice(candle.high)} x2={x} y2={yForPrice(candle.low)} /><rect x={x - bodyWidth / 2} y={Math.min(openY, closeY)} width={bodyWidth} height={Math.max(1.5, Math.abs(closeY - openY))} rx="1" /></g>;
          })}
          {candles.map((candle, index) => {
            const barWidth = Math.max(2, plotWidth / candles.length - 2);
            const barHeight = (Number(candle.volume_shares || 0) / maxVolume) * (volumeBottom - volumeTop);
            const rising = Number(candle.close) >= Number(candle.open);
            return <rect className={`chart-volume chart-volume--${rising ? "up" : "down"}`} key={candle.begin || index} x={xForIndex(index) - barWidth / 2} y={volumeBottom - barHeight} width={barWidth} height={barHeight} rx="1" />;
          })}
          <line className="chart-volume-base" x1="0" y1={volumeBottom} x2={plotWidth} y2={volumeBottom} />
          {tickIndexes.map((index) => <text className="chart-date-label" key={index} x={xForIndex(index)} y="310" textAnchor={index === 0 ? "start" : index === candles.length - 1 ? "end" : "middle"}>{new Intl.DateTimeFormat("ru-RU", isIntraday ? { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" } : { day: "2-digit", month: "short" }).format(new Date(candles[index].begin))}</text>)}
          {hoveredIndex !== null && <g className="chart-crosshair"><line x1={xForIndex(activeIndex)} y1={plotTop} x2={xForIndex(activeIndex)} y2={volumeBottom} /><circle cx={xForIndex(activeIndex)} cy={yForPrice(activeCandle.close)} r="4" /></g>}
          {eventClusters.map((cluster) => (
            <g
              className={`chart-event chart-event--${cluster.direction} ${cluster.key === selectedCluster?.key ? "is-selected" : ""}`}
              key={cluster.key}
              role="button"
              tabIndex="0"
              aria-label={`Открыть ${cluster.newsCount} ${cluster.newsCount === 1 ? "новость" : "новости"}. Самая сильная: ${cluster.strongest.title}`}
              onClick={(clickEvent) => { clickEvent.stopPropagation(); setSelectedClusterKey(cluster.key); setSelectedEventId(cluster.strongest.id); }}
              onKeyDown={(keyboardEvent) => { if (keyboardEvent.key === "Enter" || keyboardEvent.key === " ") { setSelectedClusterKey(cluster.key); setSelectedEventId(cluster.strongest.id); } }}
            >
              <title>{cluster.newsCount > 1 ? `${cluster.newsCount} новости. Максимальный вес: ${cluster.strongest.signal ? formatScore(cluster.strongest.signal.score) : "фон"}` : cluster.strongest.title}</title>
              <line x1={cluster.x} y1={cluster.y + cluster.radius} x2={cluster.x} y2={Math.min(plotBottom, cluster.y + cluster.radius + 15)} />
              <circle cx={cluster.x} cy={cluster.y} r={cluster.radius} />
              {cluster.newsCount > 1 && <text x={cluster.x} y={cluster.y + 2.7} textAnchor="middle">{cluster.newsCount}</text>}
              {cluster.newsCount > 1 && cluster.strongest.signal && <text className="chart-event-weight" x={cluster.x + cluster.radius + 4} y={cluster.y - cluster.radius + 3}>{formatScore(cluster.strongest.signal.score)}</text>}
            </g>
          ))}
        </svg>
      </div>
      <div className="chart-period-summary"><span>За период <strong className={periodChange >= 0 ? "market-positive" : "market-negative"}>{formatPct(periodChange)}</strong></span><span><strong>{eventClusters.reduce((sum, cluster) => sum + cluster.newsCount, 0)}</strong> новостей в <strong>{eventClusters.length}</strong> точках</span><span>Наведи на график или используй ← →</span></div>
      {selectedCluster && <div className="chart-event-cluster">
        <header><span>{selectedCluster.newsCount > 1 ? `${selectedCluster.newsCount} новости на одной свече` : "Новость на свече"}</span><small>Сверху — самая весомая</small></header>
        {selectedCluster.events.map((item, index) => <button type="button" className={`chart-event-detail chart-event-detail--${item.signal?.direction || "neutral"} ${item.id === selectedEvent?.id ? "is-active" : ""}`} key={item.id} onClick={() => { setSelectedEventId(item.id); onOpen?.(item); }}>
          <i />
          <div><span>{index === 0 ? "Главная · формирует сигнал" : item.signal ? "Подтверждает" : "Фон"} · {item.source}{item.signal ? ` · ${formatScore(item.signal.score)} п.` : ""}</span><strong>{item.title}</strong></div>
          <span className="chart-event-detail__open">Читать <ArrowUpRight size={12} /></span>
        </button>)}
      </div>}
      <div className="chart-legend"><span><i className="legend-price" /> Цена</span><span><i className="legend-volume" /> Объём</span><span><i className="legend-event" /> Новости: зелёный — позитив, красный — негатив, серый — фон; размер — вес</span></div>
    </div>
  );
}

function AppHeader({ view, signals, onNavigate, onSelect, onOpenNews }) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const searchUniverse = useMemo(() => {
    const activeTickers = new Set(signals.map((signal) => signal.ticker));
    const waitingCompanies = Object.entries(companyMeta)
      .filter(([ticker]) => !activeTickers.has(ticker))
      .map(([ticker, [company, sector]]) => ({ ticker, company, sector, available: false }));
    return [...signals, ...waitingCompanies];
  }, [signals]);
  const searchResults = searchUniverse.filter((signal) =>
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

  const chooseResult = (signal) => {
    if (signal.available === false) onOpenNews(signal.ticker);
    else onSelect(signal.ticker);
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
          <span className="brand-wordmark" aria-hidden="true">
            <span>EVENTE</span><strong>D</strong><span>GE</span>
          </span>
        </button>

        <nav className={`primary-nav ${mobileOpen ? "is-open" : ""}`} aria-label="Основная навигация">
          <button type="button" className={view === "signals" || view === "signal" ? "is-active" : ""} onClick={() => navigate("signals")}><CircleGauge size={14} /> Сигналы</button>
          <button type="button" className={view === "evals" ? "is-active" : ""} onClick={() => navigate("evals")}><Activity size={14} /> Evals</button>
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
              {searchResults.slice(0, 6).map((signal, index) => {
                const available = signal.available !== false;
                return (
                  <button type="button" key={signal.ticker} className={index === 0 ? "is-active" : ""} onClick={() => chooseResult(signal)}>
                    <CompanyMark signal={signal} small />
                    <span><strong>{signal.ticker}</strong><small>{signal.company} · {signal.sector}</small></span>
                    {available ? <Direction direction={signal.displayDirection || signal.direction} label={signal.direction === "neutral" && signal.displayDirection !== "neutral" ? `Уклон ${signal.displayDirection === "up" ? "вверх" : "вниз"}` : null} /> : <span className="waiting-badge"><i /> Наблюдение</span>}
                    <em>{available ? `${formatScore(signal.score)} п.` : "Лента"}</em>
                  </button>
                );
              })}
            </div>
            <footer><span>Нажми на компанию, чтобы открыть сигнал</span><span>⌘ K — поиск</span></footer>
          </section>
        </div>
      )}
    </>
  );
}

function SignalsScreen({ signals, assessmentMeta, marketStatus, marketUpdatedAt, onSelect, onOpenNews, onMethodology, onReadNews }) {
  const [filter, setFilter] = useState("all");
  const [query, setQuery] = useState("");
  const [filterOpen, setFilterOpen] = useState(false);
  const [sortByConfidence, setSortByConfidence] = useState(false);

  const companyCards = useMemo(() => {
    const activeTickers = new Set(signals.map((signal) => signal.ticker));
    const waitingCards = Object.entries(companyMeta)
      .filter(([ticker]) => !activeTickers.has(ticker))
      .map(([ticker, [company, sector]]) => ({
        ticker,
        company,
        sector,
        available: false,
        direction: null,
        score: null,
        confidence: 0,
        horizon: "—",
        updated: "ожидаем событие",
        summary: "Активного сигнала нет. Компания остаётся в наблюдении до появления новой существенной информации.",
        evidence: [],
      }));
    return [...signals, ...waitingCards];
  }, [signals]);

  const filteredSignals = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase("ru-RU");
    const result = companyCards.filter((signal) => {
      const matchesFilter = filter === "all" || (signal.displayDirection || signal.direction) === filter;
      const matchesQuery = !normalized || `${signal.ticker} ${signal.company} ${signal.sector}`.toLocaleLowerCase("ru-RU").includes(normalized);
      return matchesFilter && matchesQuery;
    });
    return [...result].sort((a, b) => {
      if (sortByConfidence) return b.confidence - a.confidence;
      return Number(b.available !== false) - Number(a.available !== false);
    });
  }, [companyCards, filter, query, sortByConfidence]);

  return (
    <main className="screen screen--signals">
      <section className="terminal-window">
        <div className="terminal-toolbar">
          <div className="terminal-title">
            <div><span className="workspace-kicker">Live intelligence</span><h1>Компании в фокусе</h1></div>
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
          <span><i className={marketStatus === "error" ? "is-error" : ""} /> MOEX ISS · {marketStatus === "loading" && !marketUpdatedAt ? "загружаем котировки" : marketUpdatedAt ? `обновлено ${formatRelative(marketUpdatedAt)}` : "данные временно недоступны"}</span>
          <span>Модель <strong>hybrid-market-0.1.0</strong></span>
          <span>Шкала сигнала <strong>от −100 до +100</strong></span>
          <span className="market-strip__right"><strong>{assessmentMeta.directed || 0}</strong> сильных · {assessmentMeta.market_biases || 0} с уклоном · {assessmentMeta.news_backed || 0} с новостью</span>
        </div>

        <div className="company-card-area">
          <div className="company-card-grid">
            {filteredSignals.map((signal) => {
              const available = signal.available !== false;
              const displayDirection = signal.displayDirection || signal.direction;
              const directionLabel = signal.direction === "neutral" && displayDirection !== "neutral" ? `Уклон ${displayDirection === "up" ? "вверх" : "вниз"}` : null;
              const evidence = signal.evidence?.[0];
              const openCard = () => available ? onSelect(signal.ticker) : onOpenNews(signal.ticker);
              return (
                <article
                  className={`company-signal-card ${available ? `company-signal-card--${displayDirection}` : "company-signal-card--waiting"}`}
                  key={signal.ticker}
                  role="button"
                  tabIndex={0}
                  onClick={openCard}
                  onKeyDown={(event) => { if (event.target === event.currentTarget && (event.key === "Enter" || event.key === " ")) openCard(); }}
                >
                  <span className="company-signal-card__glow" aria-hidden="true" />
                  <header>
                    <span className="company-signal-card__identity"><CompanyMark signal={signal} /><span><strong>{signal.ticker}</strong><small>{signal.company}</small></span></span>
                    {available ? <Direction direction={displayDirection} label={directionLabel} /> : <span className="waiting-badge"><i /> Наблюдение</span>}
                  </header>
                  <div className="company-signal-card__body">
                    <div className="company-signal-card__score">
                      <span>{available ? signal.assessment_type === "hybrid" ? "Гибридный сигнал" : "Оценка рынка" : signal.sector}</span>
                      {available ? <strong className={`score-cell--${displayDirection}`}>{formatScore(signal.score)}<small> / 100</small></strong> : <strong>Нет сигнала</strong>}
                    </div>
                    <p>{signal.summary}</p>
                    {available && <span className={`company-signal-card__action company-signal-card__action--${signal.direction}`}><Check size={11} /> {signal.action}</span>}
                  </div>
                  {available && (
                    <div className="company-signal-card__scenario">
                      <span><small>Сценарий движения</small><strong>{formatScenario(signal.scenario)}</strong></span>
                      <span><small>Цена MOEX</small><strong>{signal.price} <em>{signal.change}</em></strong></span>
                    </div>
                  )}
                  {available && <EventBubbles events={signal.evidence} onOpen={onReadNews} compact />}
                  <div className="company-signal-card__metrics">
                    <span><small>Уверенность оценки</small><strong>{available ? `${signal.confidence}%` : "—"}</strong></span>
                    <span><small>Горизонт</small><strong>{signal.horizon}</strong></span>
                    <span><small>Обновлено</small><strong>{signal.updated}</strong></span>
                  </div>
                  <footer>
                    <span>{evidence ? <Newspaper size={12} /> : <BarChart3 size={12} />} {evidence ? evidence.title : available ? signal.assessment_type === "quant" ? "Открыть рыночные факторы" : "Открыть расчёт сигнала" : "Посмотреть ленту компании"}</span>
                    <ArrowUpRight size={15} />
                  </footer>
                </article>
              );
            })}
          </div>

          {!filteredSignals.length && (
            <div className="empty-state">
              <CircleGauge size={22} />
              <strong>Компании не найдены</strong>
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

function CompanyScreen({ signal, companyNews, marketStatus, marketUpdatedAt, onBack, onMethodology, onOpenNews, onReadNews }) {
  const factors = signal.factors;
  const chartEvents = companyNews.length ? companyNews : signal.evidence;
  const [intradaySeries, setIntradaySeries] = useState([]);
  const [intradayStatus, setIntradayStatus] = useState("loading");

  useEffect(() => {
    const controller = new AbortController();
    let refreshing = false;
    const refreshIntraday = async () => {
      if (document.visibilityState === "hidden" || refreshing) return;
      refreshing = true;
      try {
        const response = await fetch(apiUrl(`/v1/instruments/${signal.ticker}/candles?interval=10&lookback_days=14`), { signal: controller.signal });
        if (!response.ok) throw new Error("MOEX candles unavailable");
        const payload = await response.json();
        setIntradaySeries(payload.data?.candles || []);
        setIntradayStatus("ready");
      } catch (error) {
        if (error.name !== "AbortError") setIntradayStatus("error");
      } finally {
        refreshing = false;
      }
    };
    setIntradayStatus("loading");
    refreshIntraday();
    const interval = window.setInterval(refreshIntraday, 60000);
    const handleVisibility = () => { if (document.visibilityState === "visible") refreshIntraday(); };
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      controller.abort();
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [signal.ticker]);

  return (
    <main className="screen screen--company">
      <div className="company-header">
        <button className="back-button" type="button" onClick={onBack} aria-label="Вернуться к сигналам"><ArrowLeft size={19} /></button>
        <CompanyMark signal={signal} />
        <div className="company-identity">
          <strong>{signal.ticker}</strong>
          <span>{signal.company} · MOEX</span>
        </div>
        <div className="company-header__signal"><Direction direction={signal.displayDirection || signal.direction} label={signal.direction === "neutral" && signal.displayDirection !== "neutral" ? `Уклон ${signal.displayDirection === "up" ? "вверх" : "вниз"}` : null} /><span>{signal.horizon}</span></div>
      </div>

      <section className="company-canvas">
        <div className="chart-card">
          <div className="chart-summary">
            <div>
              <span>Цена MOEX</span>
              <strong>{signal.price}</strong>
              <em className={Number(signal.market?.daily_change_pct) >= 0 ? "market-positive" : "market-negative"}>{signal.change} за сессию</em>
            </div>
            <div className="chart-signal">
              <Direction direction={signal.direction} />
              <span>{formatScenario(signal.scenario)} · {signal.horizon}</span>
            </div>
          </div>
          <PriceChart dailySeries={signal.series} intradaySeries={intradaySeries} intradayStatus={intradayStatus} direction={signal.direction} events={chartEvents} onOpen={onReadNews} />
          <div className="market-facts">
            <span><small>Объём</small><strong>{formatCompact(signal.market?.volume_shares)} акций</strong></span>
            <span><small>Оборот</small><strong>{formatCompact(signal.market?.value_rub)} ₽</strong></span>
            <span><small>Дневная волатильность</small><strong>{formatPct(signal.market?.daily_volatility_pct, { sign: false })}</strong></span>
            <span><small>Ликвидность</small><strong>{signal.market?.liquidity_status === "sufficient" ? "Достаточная" : signal.market?.liquidity_status === "limited" ? "Ограниченная" : "Нет данных"}</strong></span>
          </div>
          <footer className="market-source"><span>Источник: <a href={signal.market?.source?.url || "https://iss.moex.com/iss/"} target="_blank" rel="noreferrer">MOEX ISS <ArrowUpRight size={10} /></a> · 10-минутные и дневные свечи</span><span className={`market-refresh market-refresh--${marketStatus}`}><i /> {marketUpdatedAt ? `Котировка ${formatRelative(marketUpdatedAt)}` : marketStatus === "loading" ? "Обновляем…" : "Ожидаем данные"}</span></footer>
        </div>

        <div className="analysis-grid">
          <section className="decision-card">
            <div className="section-kicker"><CircleGauge size={14} /> {signal.assessment_type === "hybrid" ? "Гибридный сигнал" : "Рыночная оценка без свежей сильной новости"}</div>
            <div className="decision-headline">
              <div>
                <h1>{signal.action}</h1>
                <p>{signal.summary}</p>
              </div>
              <div className="decision-score">
              <strong className={`score-cell--${signal.displayDirection || signal.direction}`}>{formatScore(signal.score)}</strong>
                <span>пунктов из 100</span>
              </div>
            </div>
            <button className="score-explainer" type="button" onClick={onMethodology}>
              <Info size={14} />
              <span><strong>Что означают пункты?</strong> Это сила и направление гипотезы, не ожидаемая доходность.</span>
              <ArrowUpRight size={13} />
            </button>
            <div className="decision-stats">
              <div><span>Уверенность оценки</span><strong>{signal.confidence}%</strong><small>{signal.confidence >= 70 ? "высокая" : signal.confidence >= 60 ? "средняя" : "ограниченная"}</small></div>
              <div><span>Горизонт</span><strong>{signal.horizon}</strong><small>торговых</small></div>
              <div><span>Сценарий движения</span><strong>{formatScenario(signal.scenario)}</strong><small>не ценовой таргет</small></div>
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
                  <div><span>{factor.label}</span><strong className={factor.contribution >= 0 ? "factor--positive" : "factor--negative"}>{formatScore(factor.contribution)} п.</strong></div>
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
                  <p>{item.sourceCount > 1 ? `${item.sourceCount} подтверждающих источника · ` : ""}{signal.summary}</p>
                </div>
                <span className="news-time"><Clock3 size={12} /> {item.time}</span>
                <span className="news-open"><BookOpen size={14} /></span>
              </button>
            ))}
            {!signal.evidence.length && <div className="empty-state"><Newspaper size={22} /><strong>Источник не найден</strong><span>Новость могла быть удалена или ещё не загружена в ленту.</span></div>}
          </div>
        </section>

        <p className="disclaimer">Сигнал является аналитической гипотезой и не является индивидуальной инвестиционной рекомендацией.</p>
      </section>
    </main>
  );
}

function NewsScreen({ signals, allNews, newsMeta, initialTicker, onReadNews }) {
  const [ticker, setTicker] = useState(initialTicker || "all");
  const [query, setQuery] = useState("");

  useEffect(() => setTicker(initialTicker || "all"), [initialTicker]);

  const items = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase("ru-RU");
    return allNews.filter((item) => {
      const matchesTicker = ticker === "all" || item.tickers?.includes(ticker);
      const haystack = `${item.title} ${item.source} ${(item.tickers || []).join(" ")} ${item.signal?.company || item.companySignal?.company || ""}`.toLocaleLowerCase("ru-RU");
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
      <div className="pipeline-status"><span><i /> Быстрый сбор работает</span><strong>Приоритетные источники проверяются каждую минуту</strong><small>{newsMeta.last_ingested_at ? `Последняя новая запись ${formatRelative(newsMeta.last_ingested_at)} · цель доставки до ${Math.round((newsMeta.delivery_target_seconds || 120) / 60)} мин` : "Ожидаем первую публикацию"}</small></div>
      <section className="news-feed">
        <div className="feed-heading"><span>{ticker === "all" && !query ? newsMeta.total || items.length : items.length} публикаций</span><small>{newsMeta.sources?.length || 0} активных источника · события отделены от фоновых новостей</small></div>
        {items.map((item) => (
          <button type="button" className="feed-item" key={item.id} onClick={() => onReadNews(item)}>
            {item.signal || item.companySignal ? <CompanyMark signal={item.signal || item.companySignal} /> : <span className="company-mark"><Newspaper size={17} /></span>}
            <div className="feed-copy"><div><span>{item.source}</span><i>{item.tag}</i>{item.sourceCount > 1 && <i className="source-count">{item.sourceCount} источника</i>}<time>{item.time}</time></div><h2>{item.title}</h2><p>{item.content}</p>{item.signal && <footer><strong>{item.signal.ticker}</strong><Direction direction={item.signal.direction} /><span>{formatScore(item.signal.score)} п.</span></footer>}</div>
            <BookOpen size={17} />
          </button>
        ))}
        {!items.length && <div className="empty-state"><Search size={22} /><strong>Новостей не найдено</strong><span>Измени запрос или выбери другую компанию.</span></div>}
      </section>
    </main>
  );
}

function EvalsScreen() {
  const [payload, setPayload] = useState(null);
  const [status, setStatus] = useState("loading");
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    let refreshing = false;
    const load = async () => {
      if (refreshing || document.visibilityState === "hidden") return;
      refreshing = true;
      try {
        const response = await fetch(apiUrl("/v1/evals"), { signal: controller.signal });
        if (!response.ok) throw new Error("Evals API временно недоступен.");
        setPayload(await response.json());
        setStatus("ready");
        setError("");
      } catch (loadError) {
        if (loadError.name !== "AbortError") {
          setStatus("error");
          setError(loadError.message || "Не удалось загрузить Evals.");
        }
      } finally {
        refreshing = false;
      }
    };
    load();
    const interval = window.setInterval(load, 60000);
    const handleVisibility = () => { if (document.visibilityState === "visible") load(); };
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      controller.abort();
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, []);

  if (status !== "ready") return <DataState error={status === "error" ? error : ""} onRetry={() => window.location.reload()} />;

  const { summary, demo_account: account, outcomes } = payload.data;
  const hitRate = summary.hit_rate_pct === null ? "—" : `${summary.hit_rate_pct}%`;
  const averageReturn = summary.average_signed_return_pct === null ? "—" : formatPct(summary.average_signed_return_pct);

  return (
    <main className="screen section-screen evals-screen">
      <section className="page-hero evals-hero">
        <div><span className="eyebrow"><Activity size={13} /> Проверка реальностью</span><h1>Evals: что произошло после сигнала</h1><p>Система сопоставляет сигнал с первой доступной свечой MOEX, проверяет движение через 1 час, 1 день и 3 дня и теми же правилами ведёт прозрачный демо‑счёт.</p></div>
        <div className="eval-live"><i /><span><strong>Обновляется раз в минуту</strong><small>{formatRelative(payload.meta.generated_at)} · окно 10‑минутных свечей</small></span></div>
      </section>

      <section className="eval-warning"><ShieldCheck size={17} /><div><strong>Это технический eval, а не доказательство доходности</strong><span>Выборка пока мала и не является point‑in‑time калиброванным backtest. Результаты нужны, чтобы находить слабые места модели до использования капитала.</span></div></section>

      <section className="eval-kpis">
        <article><span>Проверено сигналов</span><strong>{summary.evaluated}</strong><small>из {summary.signals_total} доступных в хранилище</small></article>
        <article><span>Попадание направления</span><strong>{hitRate}</strong><small>по самому длинному доступному горизонту</small></article>
        <article><span>Средняя реакция</span><strong className={Number(summary.average_signed_return_pct) >= 0 ? "market-positive" : "market-negative"}>{averageReturn}</strong><small>доходность со знаком сигнала, до издержек</small></article>
        <article><span>Покрытие eval</span><strong>{summary.coverage_pct}%</strong><small>{summary.pending} ждут 3 дня · {summary.unavailable} вне окна</small></article>
      </section>

      <section className="demo-account">
        <header className="demo-account__header">
          <div><span className="section-kicker"><WalletCards size={14} /> Демо‑счёт</span><h2>{formatCurrency(account.equity_rub)}</h2><p>Старт {formatCurrency(account.initial_balance_rub)} · только канонические news‑сигналы · без реальных заявок</p></div>
          <div className={`demo-return ${account.net_return_pct >= 0 ? "is-positive" : "is-negative"}`}><span>Результат</span><strong>{formatPct(account.net_return_pct)}</strong><small>комиссии {formatCurrency(account.total_commission_rub)}</small></div>
        </header>
        <div className="demo-rules">
          <span><strong>{account.rules.position_share_pct}%</strong> счёта на сигнал</span>
          <span><strong>{account.rules.commission_per_side_pct}%</strong> комиссия за сторону</span>
          <span><strong>{account.rules.slippage_per_side_pct}%</strong> проскальзывание</span>
          <span><strong>{account.closed_trades}</strong> закрыто · {account.open_positions} открыто · {account.skipped_signals} пропущено</span>
        </div>
        <div className="eval-table-wrap">
          <table className="eval-table demo-trades">
            <thead><tr><th>Бумага</th><th>Сделка</th><th>Вход</th><th>Выход / mark</th><th>Издержки</th><th>Результат</th><th>Статус</th></tr></thead>
            <tbody>
              {account.trades.slice(0, 12).map((trade) => <tr key={`${trade.signal_id}-${trade.opened_at}`}>
                <td><strong>{trade.ticker}</strong></td>
                <td><span className={`trade-side trade-side--${trade.side}`}>{trade.side === "long" ? "LONG" : "SHORT"}</span></td>
                <td>{formatPrice(trade.entry_price)}</td>
                <td>{formatPrice(trade.exit_or_mark_price)}</td>
                <td>{formatCurrency(trade.commission_rub)}</td>
                <td className={trade.pnl_rub >= 0 ? "market-positive" : "market-negative"}><strong>{formatCurrency(trade.pnl_rub)}</strong><small>{formatPct(trade.return_pct)}</small></td>
                <td><span className={`trade-status trade-status--${trade.status}`}>{trade.status === "closed" ? "Закрыта" : "Открыта"}</span></td>
              </tr>)}
            </tbody>
          </table>
          {!account.trades.length && <div className="empty-state"><WalletCards size={22} /><strong>Сделок пока нет</strong><span>Демо‑счёт открывает позиции только по направленным новостным сигналам.</span></div>}
        </div>
      </section>

      <section className="outcomes-card">
        <div className="section-heading"><span><BarChart3 size={15} /> Реакция после каждого сигнала</span><small>цена от первой торгуемой свечи</small></div>
        <div className="eval-table-wrap">
          <table className="eval-table outcomes-table">
            <thead><tr><th>Сигнал</th><th>Новость</th><th>1 час</th><th>1 день</th><th>3 дня</th><th>Вердикт</th></tr></thead>
            <tbody>
              {outcomes.slice(0, 30).map((outcome) => <tr key={outcome.signal_id}>
                <td><div className="outcome-signal"><strong>{outcome.ticker}</strong><Direction direction={outcome.direction} /><small>{formatScore(outcome.score)} п.</small></div></td>
                <td>{outcome.news ? <a href={outcome.news.url} target="_blank" rel="noreferrer"><span>{outcome.news.source_id}</span><strong>{outcome.news.title}</strong></a> : <span>Источник недоступен</span>}</td>
                {["1h", "1d", "3d"].map((period) => {
                  const value = outcome.returns?.[period];
                  return <td key={period} className={value === null || value === undefined ? "" : Number(value) >= 0 ? "market-positive" : "market-negative"}>{formatPct(value)}</td>;
                })}
                <td>{outcome.verdict === null ? <span className="eval-verdict is-pending">Ждём данные</span> : outcome.verdict ? <span className="eval-verdict is-hit"><Check size={11} /> Попал</span> : <span className="eval-verdict is-miss"><X size={11} /> Не попал</span>}</td>
              </tr>)}
            </tbody>
          </table>
        </div>
      </section>
    </main>
  );
}

function MethodologyScreen({ onApi, allNews, newsMeta }) {
  const sample = methodologySignals[0];
  const sampleFactors = scoreFactors(sample.score);
  const recentBySource = useMemo(() => {
    const result = new Map();
    allNews.forEach((item) => {
      (item.sources || [{ id: item.sourceId, publishedAt: item.publishedAt }]).forEach((source) => {
        if (!result.has(source.id)) result.set(source.id, source.publishedAt);
      });
    });
    return result;
  }, [allNews]);

  return (
    <main className="screen section-screen methodology-screen">
      <section className="page-hero methodology-hero">
        <div><span className="eyebrow"><BookOpen size={13} /> Прозрачная методика</span><h1>Как считается сигнал</h1><p>LLM не предсказывает цену напрямую. Она разбирает новость, а независимый рыночный слой проверяет реакцию цены, объём, риск и доступную отчётность. Итог собирает детерминированная формула.</p></div>
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

      <section className="quant-method">
        <header><span>04</span><div><h2>Не‑LLM слой проверяет рынок</h2><p>Даже без свежей сильной новости по каждой бумаге остаётся live‑оценка. Если новостной сигнал есть, итог считается по фиксированной пропорции 65% news / 35% market.</p></div></header>
        <div className="quant-factor-grid">
          <article><strong>Реакция цены</strong><span>Движение за сессию и пять дней</span><b>направление</b></article>
          <article><strong>Объём</strong><span>Отклонение от медианы сессий</span><b>подтверждение</b></article>
          <article><strong>Волатильность</strong><span>Реализованный дневной риск</span><b>уверенность</b></article>
          <article><strong>Ликвидность</strong><span>Оборот и исполнимость идеи</span><b>уверенность</b></article>
          <article><strong>Отчётность</strong><span>Детерминированные факты из раскрытия</span><b>направление</b></article>
        </div>
        <code>live score = news score × 0,65 + market score × 0,35</code>
      </section>

      <section className="scenario-method">
        <div><span>05</span><div><h2>Рынок задаёт диапазон движения</h2><p>Дневная волатильность из 30 свечей MOEX масштабируется на горизонт сигнала. Направление и сила оценки сдвигают диапазон вверх, вниз или вокруг нуля.</p></div></div>
        <code>диапазон = σ дневная × √горизонт × сила сигнала</code>
        <p><ShieldCheck size={14} /> Это сценарная зона, а не таргет цены и не обещанная доходность. Калибровка на исторических outcomes остаётся следующим этапом.</p>
      </section>

      <section className="source-method">
        <div className="section-heading"><span><Database size={15} /> Источники и их роль</span><small>прозрачный реестр текущего контура</small></div>
        <div className="source-method__grid">
          {methodologySources.map((source) => {
            const lastSeen = recentBySource.get(source.id);
            const sourceStat = newsMeta.sources?.find((item) => item.source_id === source.id);
            const hasData = source.id === "moex_iss" || Boolean(lastSeen);
            return (
              <a href={source.url} target="_blank" rel="noreferrer" key={source.id} className="source-card">
                <header><span>{source.kind}</span><strong>{source.quality}/100</strong></header>
                <h3>{source.name}<ArrowUpRight size={12} /></h3>
                <p>{source.role}</p>
                <footer><span className={hasData ? "is-live" : "is-waiting"}><i /> {hasData ? sourceStat ? `${sourceStat.count} публикаций` : "Есть данные" : "Подключён · без событий"}</span><time>{lastSeen ? formatRelative(lastSeen) : source.freshness}</time></footer>
              </a>
            );
          })}
        </div>
        <p className="source-note">Качество источника — фиксированный вес внутри текущей модели. Discovery-источник помогает найти публикацию, но не заменяет первичное подтверждение.</p>
      </section>

      <section className="method-reality">
        <div><ShieldCheck size={17} /><span><strong>Что работает сейчас</strong>Новостной baseline, пять не‑LLM факторов, live‑оценка 15 бумаг, outcomes и демо‑счёт с издержками.</span></div>
        <div><Database size={17} /><span><strong>Граница текущей версии</strong>Отчётность пока извлекается из распознанных раскрытий; полноценный point‑in‑time фундаментальный датасет и калиброванный backtest ещё не готовы.</span></div>
        <button type="button" onClick={onApi}>Посмотреть API <ArrowUpRight size={13} /></button>
      </section>
    </main>
  );
}

const apiEndpoints = [
  { id: "assessments", method: "GET", path: "/v1/assessments", title: "Live‑оценки рынка", description: "Гибридная или quant‑оценка всех компаний с пятью не‑LLM факторами.", parameter: { name: "tickers", type: "string", description: "Опциональный список тикеров MOEX через запятую" } },
  { id: "evals", method: "GET", path: "/v1/evals", title: "Outcomes и демо‑счёт", description: "Реакция цены через 1ч/1д/3д, метрики качества и демо‑портфель с издержками.", parameter: null },
  { id: "signals", method: "GET", path: "/v1/signals?limit=20", title: "Новостные сигналы", description: "Канонические сигналы, созданные существенными новостными событиями.", parameter: { name: "limit", type: "integer", description: "Количество записей, максимум 100" } },
  { id: "news", method: "GET", path: "/v1/news?limit=20", title: "Лента новостей", description: "Исходные публикации и сигналы, которые с ними связаны.", parameter: { name: "limit", type: "integer", description: "Количество публикаций, максимум 100" } },
  { id: "ticker", method: "GET", path: "/v1/signals?ticker=SBER&limit=1", title: "Сигнал компании", description: "Последний новостной сигнал по выбранному тикеру.", parameter: { name: "ticker", type: "string", description: "Тикер MOEX, например SBER" } },
  { id: "snapshot", method: "GET", path: "/v1/instruments/SBER/snapshot", title: "Рыночный snapshot", description: "Цена, объём, волатильность, дневные свечи MOEX и сценарный диапазон.", parameter: null },
  { id: "candles", method: "GET", path: "/v1/instruments/SBER/candles?interval=10&lookback_days=14", title: "Внутридневные свечи", description: "10-минутные OHLCV-свечи MOEX. Кэш и интерфейс обновляются раз в минуту.", parameter: { name: "lookback_days", type: "integer", description: "Окно истории от 1 до 14 дней" } },
  { id: "snapshots", method: "GET", path: "/v1/instruments/snapshots?tickers=SBER,LKOH,YDEX", title: "Котировки списком", description: "До 20 рыночных snapshot одним запросом.", parameter: { name: "tickers", type: "string", description: "Список тикеров MOEX через запятую" } },
  { id: "health", method: "GET", path: "/health/ready", title: "Готовность сервиса", description: "Проверка приложения и соединения с хранилищем.", parameter: null },
];

function ApiScreen() {
  const [selectedId, setSelectedId] = useState("assessments");
  const [status, setStatus] = useState("idle");
  const [copied, setCopied] = useState("");
  const endpoint = apiEndpoints.find((item) => item.id === selectedId);
  const baseUrl = typeof window === "undefined" ? "" : window.location.origin;
  const responseExample = endpoint.id === "health" ? `{"status":"ok"}` : endpoint.id === "assessments" ? `{
  "data": [{
    "ticker": "SBER",
    "assessment_type": "hybrid",
    "direction": "up",
    "score": 31.4,
    "factor_contributions": [
      {"code":"news_signal","contribution":25.4},
      {"code":"price_reaction","contribution":6.0}
    ]
  }],
  "meta": {"returned":15,"directed":2,"market_biases":11,"news_backed":3}
}` : endpoint.id === "evals" ? `{
  "data": {
    "summary": {"evaluated":12,"hit_rate_pct":58.3},
    "demo_account": {"initial_balance_rub":1000000,"net_return_pct":1.24},
    "outcomes": [{"ticker":"SBER","returns":{"1h":0.4,"1d":1.2,"3d":2.1},"verdict":true}]
  }
}` : endpoint.id === "snapshot" ? `{
  "data": {
    "ticker": "SBER",
    "market": {
      "last_price": "283.65",
      "daily_change_pct": 0.41,
      "daily_volatility_pct": 1.42,
      "source": {"name": "MOEX ISS"}
    },
    "scenario": {
      "low_pct": 0.74,
      "high_pct": 2.48,
      "label": "Сценарный диапазон, не таргет"
    }
  }
}` : endpoint.id === "candles" ? `{
  "data": {
    "ticker": "SBER",
    "interval_minutes": 10,
    "candles": [{"begin":"2026-08-07T07:00:00Z","open":283.1,"high":284.0,"low":282.9,"close":283.8,"volume_shares":184220}]
  },
  "meta": {"refresh_after_seconds":60}
}` : endpoint.id === "snapshots" ? `{
  "data": [
    {"ticker":"SBER","market":{"last_price":"283.65"}},
    {"ticker":"LKOH","market":{"last_price":"6714.0"}}
  ],
  "errors": [],
  "meta": {"requested":2,"returned":2,"refresh_after_seconds":30}
}` : endpoint.id === "news" ? `{
  "data": [{
    "id": "news_…",
    "source_id": "moex_news",
    "title": "Сообщение эмитента",
    "url": "https://www.moex.com/n…",
    "related_signals": [{"ticker":"SBER","direction":"up","score":24.8}]
  }],
  "meta": {
    "limit":20,
    "total":84,
    "has_more":true,
    "poll_interval_seconds":60,
    "client_refresh_interval_seconds":30,
    "delivery_target_seconds":120,
    "collection_lanes":[{"id":"fast","interval_seconds":60,"source_ids":["interfax","tass","rbc","moex_news"]}],
    "sources":[{"source_id":"interfax","count":24,"signal_count":5}]
  }
}` : `{
  "data": [
    {
      "id": "sig_01JZK6K5GDX90Q2X8C0R4D7M9P",
      "ticker": "SBER",
      "direction": "up",
      "score": 42.7,
      "confidence": 0.76,
      "horizon": {"value": 3, "unit": "calendar_days"},
      "model_version": "news-baseline-0.1.1"
    }
  ],
  "meta": {"limit": 20, "has_more": false, "next_cursor": null}
}`;

  const checkApi = async () => {
    setStatus("checking");
    try {
      const response = await fetch(apiUrl("/health/live"), { headers: { Accept: "application/json" } });
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
        <div><span className="eyebrow"><Braces size={13} /> EventEdge API</span><h1>Получай сигналы через простой HTTP API</h1><p>Интерфейс для внутренних продуктов, аналитических пайплайнов и автоматизации. Доступно чтение сигналов, исходных новостей и health-check.</p></div>
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
          {endpoint.parameter && <div className="parameter-table"><div><strong>Параметр</strong><strong>Тип</strong><strong>Описание</strong></div><div><code>{endpoint.parameter.name}</code><span>{endpoint.parameter.type}</span><p>{endpoint.parameter.description}</p></div></div>}
          <div className="code-panel"><div><span><Terminal size={13} /> cURL</span><button type="button" onClick={() => copyText(`curl -s '${baseUrl}${endpoint.path}'`, "curl")}><Copy size={13} /> {copied === "curl" ? "Готово" : "Копировать"}</button></div><pre><code>{`curl -s '${baseUrl}${endpoint.path}' \\\n  -H 'Accept: application/json'`}</code></pre></div>
          <div className="response-panel"><span>Пример ответа</span><pre><code>{responseExample}</code></pre></div>
        </section>
      </div>
    </main>
  );
}

function NewsReader({ item, onClose }) {
  if (!item) return null;
  const { signal } = item;
  const companySignal = signal || item.companySignal;
  return (
    <div className="reader-overlay">
      <button className="overlay-dismiss" type="button" aria-label="Закрыть новость" onClick={onClose} />
      <article className="reader-dialog" role="dialog" aria-modal="true" aria-label="Просмотр новости">
        <header><div>{companySignal ? <CompanyMark signal={companySignal} /> : <span className="company-mark"><Newspaper size={17} /></span>}<span><strong>{companySignal?.ticker || "Новость"}</strong><small>{companySignal ? `${companySignal.company} · ` : ""}{item.source}</small></span></div><button type="button" onClick={onClose} aria-label="Закрыть"><X size={18} /></button></header>
        <div className="reader-meta"><span>{item.tag}</span><time><Clock3 size={12} /> {new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" }).format(new Date(item.publishedAt))}</time></div>
        <h1>{item.title}</h1>
        <div className="reader-body"><p>{item.content}</p>{signal && <p>EventEdge связал публикацию с {signal.ticker} и алгоритмически рассчитал на горизонте {signal.horizon} оценку <strong>{formatScore(signal.score)} пункта</strong>.</p>}</div>
        {item.sources?.length > 1 && <section className="reader-sources"><span>Подтверждающие публикации</span>{item.sources.map((source) => <a href={source.url} target="_blank" rel="noreferrer" key={`${source.name}-${source.url}`}>{source.name}<ArrowUpRight size={11} /></a>)}</section>}
        {signal && <section className="reader-insight"><CircleGauge size={16} /><div><span>Что это меняет</span><strong>{signal.action}</strong><p>{signal.summary}</p></div></section>}
        <footer><FileText size={13} /> Показан текст из RSS источника. <a href={item.url} target="_blank" rel="noreferrer">Открыть оригинал <ArrowUpRight size={11} /></a></footer>
      </article>
    </div>
  );
}

function parseRoute() {
  const value = window.location.hash.replace(/^#\/?/, "") || "signals";
  const [view, ticker] = value.split("/");
  return { view: ["signals", "signal", "evals", "news", "methodology", "api"].includes(view) ? view : "signals", ticker: ticker || null };
}

function DataState({ error, onRetry }) {
  return (
    <main className="screen section-screen">
      <div className="empty-state">
        {error ? <Server size={24} /> : <RefreshCw className="is-spinning" size={24} />}
        <strong>{error ? "Не удалось загрузить данные" : "Загружаем живой контур"}</strong>
        <span>{error || "Получаем последние новости и рассчитанные сигналы."}</span>
        {error && <button type="button" className="method-link" onClick={onRetry}><RefreshCw size={13} /> Повторить</button>}
      </div>
    </main>
  );
}

export default function App() {
  const [route, setRoute] = useState(parseRoute);
  const [readerItem, setReaderItem] = useState(null);
  const [signals, setSignals] = useState([]);
  const [assessmentMeta, setAssessmentMeta] = useState({ directed: 0, market_biases: 0, news_backed: 0 });
  const [allNews, setAllNews] = useState([]);
  const [newsMeta, setNewsMeta] = useState({ total: 0, sources: [] });
  const [dataStatus, setDataStatus] = useState("loading");
  const [dataError, setDataError] = useState("");
  const [marketStatus, setMarketStatus] = useState("loading");
  const [marketUpdatedAt, setMarketUpdatedAt] = useState(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    const handleHashChange = () => setRoute(parseRoute());
    window.addEventListener("hashchange", handleHashChange);
    return () => window.removeEventListener("hashchange", handleHashChange);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let loaded = false;
    let refreshing = false;
    const load = async () => {
      if (refreshing || (loaded && document.visibilityState === "hidden")) return;
      refreshing = true;
      if (!loaded) setDataStatus("loading");
      setDataError("");
      try {
        const [signalResponse, newsResponse] = await Promise.all([
          fetch(apiUrl("/v1/signals?status=active&limit=100"), { signal: controller.signal }),
          fetch(apiUrl("/v1/news?limit=100"), { signal: controller.signal }),
        ]);
        if (!signalResponse.ok || !newsResponse.ok) throw new Error("API вернул ошибку. Попробуй обновить страницу.");
        const [signalPayload, newsPayload] = await Promise.all([signalResponse.json(), newsResponse.json()]);
        const allSignals = signalPayload.data.map(signalFromApi);
        const signalsById = new Map(allSignals.map((item) => [item.id, item]));
        const nextNews = groupNewsEvents(newsPayload.data.map((item) => newsFromApi(item, signalsById)));
        const evidenceByTicker = new Map();
        nextNews.forEach((item) => {
          if (item.signal) item.signal.evidence.push(item);
          (item.tickers || []).forEach((ticker) => {
            if (!evidenceByTicker.has(ticker)) evidenceByTicker.set(ticker, []);
            evidenceByTicker.get(ticker).push(item);
          });
        });
        const seenTickers = new Set();
        setSignals(allSignals.filter((item) => {
          if (seenTickers.has(item.ticker)) return false;
          seenTickers.add(item.ticker);
          return true;
        }));
        setAllNews(nextNews);
        setNewsMeta(newsPayload.meta || { total: nextNews.length, sources: [] });
        setDataStatus("ready");
        loaded = true;

        try {
          const assessmentResponse = await fetch(apiUrl("/v1/assessments"), { signal: controller.signal });
          if (!assessmentResponse.ok) throw new Error("Live assessments unavailable");
          const assessmentPayload = await assessmentResponse.json();
          const nextSignals = assessmentPayload.data.map(assessmentFromApi);
          setSignals(nextSignals.map((item) => ({ ...item, evidence: evidenceByTicker.get(item.ticker)?.slice(0, 8) || [] })));
          setAssessmentMeta(assessmentPayload.meta || { directed: 0, market_biases: 0, news_backed: 0 });
          setMarketUpdatedAt(new Date().toISOString());
          setMarketStatus(assessmentPayload.data.length ? "ready" : "error");
        } catch (assessmentError) {
          if (assessmentError.name !== "AbortError") setMarketStatus("error");
        }
      } catch (error) {
        if (error.name === "AbortError") return;
        if (!loaded) {
          setDataError(error.message || "Неизвестная ошибка загрузки.");
          setDataStatus("error");
        }
      } finally {
        refreshing = false;
      }
    };
    load();
    const refreshMs = 30000;
    const interval = window.setInterval(load, refreshMs);
    const handleVisibility = () => {
      if (document.visibilityState === "visible") load();
    };
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      controller.abort();
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [reloadKey]);

  const navigate = (view, ticker = null) => {
    const nextHash = `#${view}${ticker ? `/${ticker}` : ""}`;
    if (window.location.hash === nextHash) setRoute({ view, ticker });
    else window.location.hash = nextHash;
    window.scrollTo({ top: 0 });
  };

  const selectedSignal = signals.find((signal) => signal.ticker === route.ticker) || signals[0];
  const selectedCompanyNews = selectedSignal ? allNews.filter((item) => item.tickers?.includes(selectedSignal.ticker) || item.signal?.ticker === selectedSignal.ticker) : [];
  const needsData = ["signals", "signal", "news"].includes(route.view);

  return (
    <div className="app-shell">
      <AppHeader view={route.view} signals={signals} onNavigate={navigate} onSelect={(ticker) => navigate("signal", ticker)} onOpenNews={(ticker) => navigate("news", ticker)} />
      {needsData && dataStatus !== "ready" && <DataState error={dataStatus === "error" ? dataError : ""} onRetry={() => setReloadKey((value) => value + 1)} />}
      {dataStatus === "ready" && route.view === "signals" && <SignalsScreen signals={signals} assessmentMeta={assessmentMeta} marketStatus={marketStatus} marketUpdatedAt={marketUpdatedAt} onSelect={(ticker) => navigate("signal", ticker)} onOpenNews={(ticker) => navigate("news", ticker)} onMethodology={() => navigate("methodology")} onReadNews={setReaderItem} />}
      {dataStatus === "ready" && route.view === "signal" && (selectedSignal ? <CompanyScreen signal={selectedSignal} companyNews={selectedCompanyNews} marketStatus={marketStatus} marketUpdatedAt={marketUpdatedAt} onBack={() => navigate("signals")} onMethodology={() => navigate("methodology")} onOpenNews={() => navigate("news", selectedSignal.ticker)} onReadNews={setReaderItem} /> : <DataState error="Сигнал ещё не рассчитан." onRetry={() => navigate("signals")} />)}
      {route.view === "evals" && <EvalsScreen />}
      {dataStatus === "ready" && route.view === "news" && <NewsScreen signals={signals} allNews={allNews} newsMeta={newsMeta} initialTicker={route.ticker} onReadNews={setReaderItem} />}
      {route.view === "methodology" && <MethodologyScreen onApi={() => navigate("api")} allNews={allNews} newsMeta={newsMeta} />}
      {route.view === "api" && <ApiScreen />}
      <NewsReader item={readerItem} onClose={() => setReaderItem(null)} />
    </div>
  );
}
