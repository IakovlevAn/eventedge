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
  Download,
  FileText,
  Filter,
  Globe2,
  Info,
  Layers3,
  Menu,
  Minus,
  Newspaper,
  Plus,
  RefreshCw,
  Search,
  Server,
  ShieldCheck,
  SlidersHorizontal,
  Terminal,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

const API_BASE = (import.meta.env.VITE_API_BASE || "").replace(/\/$/, "");
const apiUrl = (path) => `${API_BASE}${path}`;
const SOURCE_CACHE_KEY = "eventedge:source-registry:v1";

function readCachedSourceRegistry() {
  if (typeof window === "undefined") return null;
  try {
    const cached = JSON.parse(window.localStorage.getItem(SOURCE_CACHE_KEY) || "null");
    return cached?.data?.length ? cached : null;
  } catch {
    return null;
  }
}

function cacheSourceRegistry(payload) {
  if (typeof window === "undefined" || !payload?.data?.length) return;
  try {
    window.localStorage.setItem(SOURCE_CACHE_KEY, JSON.stringify(payload));
  } catch {
    // The live response remains usable when browser storage is unavailable.
  }
}

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
  AFLT: { colors: ["#0956a0", "#d61920"], glyph: "АФ", asset: "/brands/AFLT.png" },
  NLMK: { colors: ["#006d80", "#013d49"], glyph: "НЛ", asset: "/brands/NLMK.png" },
  PHOR: { colors: ["#1675ba", "#0d477a"], glyph: "ФА", asset: "/brands/PHOR.png" },
  OZON: { colors: ["#005bff", "#003ab5"], glyph: "OZ", asset: "/brands/OZON.png" },
  X5: { colors: ["#5dbb46", "#ec7425"], glyph: "X5", asset: "/brands/X5.png" },
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
  AFLT: ["Аэрофлот", "Транспорт"], NLMK: ["НЛМК", "Металлы"],
  PHOR: ["ФосАгро", "Химия"], OZON: ["Ozon", "Ритейл"],
  X5: ["X5 Group", "Ритейл"],
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
  telegram_ak47pfl: "AK47 PFL",
  telegram_markettwits: "MarketTwits",
  telegram_centralbank_russia: "Банк России · Telegram",
  telegram_moscowexchangeofficial: "MOEX · Telegram",
  telegram_bcs_express: "БКС Экспресс",
  telegram_russianmacro: "MMI",
};

const methodologySources = [
  { id: "cbr_press", name: "Банк России", kind: "Первичный", quality: 95, freshness: "до 15 мин", role: "Макроэкономические решения и пресс-релизы регулятора", url: "https://www.cbr.ru/press/" },
  { id: "moex_news", name: "Московская биржа", kind: "Первичный", quality: 95, freshness: "цель ≤ 2 мин", role: "Сообщения биржи и эмитентов", url: "https://www.moex.com/ru/news/" },
  { id: "interfax", name: "Интерфакс", kind: "Агентство", quality: 90, freshness: "цель ≤ 2 мин", role: "Оперативные корпоративные и рыночные новости", url: "https://www.interfax.ru/business/" },
  { id: "tass", name: "ТАСС", kind: "Агентство", quality: 82, freshness: "цель ≤ 2 мин", role: "Подтверждение значимых событий", url: "https://tass.ru/ekonomika" },
  { id: "rbc", name: "РБК", kind: "Медиа", quality: 78, freshness: "цель ≤ 2 мин", role: "Рыночный контекст и дополнительное подтверждение", url: "https://www.rbc.ru/quote/" },
  { id: "google_news", name: "Google News", kind: "Discovery", quality: 74, freshness: "до 5 мин", role: "Поиск публикаций; не считается первичным источником", url: "https://news.google.com/" },
  { id: "market_background", name: "Рыночный фон", kind: "Контекст", quality: 74, freshness: "до 5 мин", role: "Ставка, рубль, нефть, санкции и общий фон рынка", url: "https://news.google.com/" },
  { id: "telegram_ak47pfl", name: "AK47 PFL", kind: "Telegram", quality: 68, freshness: "цель ≤ 2 мин", role: "Оперативные рыночные сообщения", url: "https://t.me/s/AK47pfl" },
  { id: "telegram_markettwits", name: "MarketTwits", kind: "Telegram", quality: 68, freshness: "цель ≤ 2 мин", role: "Корпоративный и рыночный поток", url: "https://t.me/s/markettwits" },
  { id: "telegram_centralbank_russia", name: "Банк России · Telegram", kind: "Telegram", quality: 95, freshness: "цель ≤ 2 мин", role: "Решения и комментарии регулятора", url: "https://t.me/s/centralbank_russia" },
  { id: "telegram_moscowexchangeofficial", name: "MOEX · Telegram", kind: "Telegram", quality: 95, freshness: "цель ≤ 2 мин", role: "Новости торгов и инфраструктуры", url: "https://t.me/s/MoscowExchangeOfficial" },
  { id: "telegram_bcs_express", name: "БКС Экспресс", kind: "Telegram", quality: 82, freshness: "цель ≤ 2 мин", role: "Корпоративные события и рынок", url: "https://t.me/s/bcs_express" },
  { id: "telegram_russianmacro", name: "MMI", kind: "Telegram", quality: 78, freshness: "цель ≤ 2 мин", role: "Российский и глобальный макро-фон", url: "https://t.me/s/russianmacro" },
  { id: "moex_iss", name: "MOEX ISS", kind: "Рыночные данные", quality: 100, freshness: "до 60 сек", role: "Цена, объём, свечи, ликвидность и волатильность", url: "https://iss.moex.com/iss/" },
];

const eventScopeMeta = {
  market: { label: "Рынок", Icon: Globe2 },
  sector: { label: "Отрасль", Icon: Layers3 },
  company: { label: "Компания", Icon: BarChart3 },
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

function formatPublicationTime(timestamp, withDate = true) {
  if (!timestamp) return "время не указано";
  return new Intl.DateTimeFormat("ru-RU", withDate
    ? { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }
    : { hour: "2-digit", minute: "2-digit" }).format(new Date(timestamp));
}

function deliveryLagMinutes(publishedAt, receivedAt) {
  if (!publishedAt || !receivedAt) return null;
  return Math.max(0, Math.round((new Date(receivedAt) - new Date(publishedAt)) / 60000));
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
    signalAt: item.as_of,
    signalCreatedAt: item.created_at,
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
    displayDirection: item.direction,
    marketBiasDirection: item.bias_direction,
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
    signalAt: item.news_signal?.as_of || item.as_of,
    signalCreatedAt: item.news_signal?.created_at || item.as_of,
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

function newsFromApi(item, signalsById, sourceNames = new Map()) {
  const relatedSignals = (item.related_signals || []).map((related) => {
    const target = related.target || { type: "instrument", id: related.ticker, label: related.ticker };
    const [company, sector] = target.type === "instrument"
      ? companyMeta[related.ticker] || [target.label, "Российский рынок"]
      : [target.label, item.event?.sectors?.join(", ") || "Российский рынок"];
    return signalsById.get(related.id) || {
      ...related,
      target,
      company,
      sector,
      confidence: Math.round(related.confidence * 100),
      horizon: "3 дн.",
      action: actionLabels[related.action] || related.action,
      summary: "Ретроспективная оценка эффекта новости на момент её публикации.",
      evidence: [],
    };
  });
  const related = relatedSignals[0];
  const instrumentSignalTickers = relatedSignals
    .filter((signal) => (signal.target?.type || "instrument") === "instrument")
    .map((signal) => signal.ticker);
  const tickers = [...new Set([
    ...(item.source_metadata?.tickers || []),
    ...instrumentSignalTickers,
  ])];
  return {
    id: item.id,
    source: sourceNames.get(item.source_id) || sourceLabels[item.source_id] || item.source_id,
    sourceId: item.source_id,
    time: formatTime(item.published_at),
    publishedAt: item.published_at,
    receivedAt: item.received_at,
    deliveryLagMinutes: deliveryLagMinutes(item.published_at, item.received_at),
    processedAt: item.created_at,
    tag: related ? related.status === "active" ? "Активный сигнал" : "Исторический сигнал" : "Без сигнала",
    title: item.title,
    content: item.content,
    url: item.url,
    signal: related || null,
    signals: relatedSignals,
    processing: item.processing || null,
    tickers,
    event: item.event || {
      scope: tickers.length ? "company" : "market",
      scope_label: tickers.length ? "Компания" : "Рынок",
      tickers,
      sectors: tickers.map((ticker) => companyMeta[ticker]?.[1]).filter(Boolean),
    },
    companySignal: !related && tickers[0] ? {
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
  if (left.event?.scope !== right.event?.scope) return false;
  const leftTicker = left.signal?.ticker || left.tickers?.[0];
  const rightTicker = right.signal?.ticker || right.tickers?.[0];
  if (leftTicker && rightTicker && leftTicker !== rightTicker) return false;
  if (!leftTicker && !rightTicker) {
    const leftSector = left.event?.sectors?.[0];
    const rightSector = right.event?.sectors?.[0];
    if (leftSector && rightSector && leftSector !== rightSector) return false;
  }
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
        sources: [{ id: item.sourceId, name: item.source, url: item.url, publishedAt: item.publishedAt, receivedAt: item.receivedAt }],
        corroborations: [],
      });
      return;
    }
    const mergedSignals = [...(group.signals || []), ...(item.signals || [])]
      .filter((signal, index, values) => values.findIndex((candidate) => candidate.id === signal.id) === index);
    const groupWeight = Math.abs(group.signal?.score || 0) + Math.min(group.content?.length || 0, 600) / 120;
    const itemWeight = Math.abs(item.signal?.score || 0) + Math.min(item.content?.length || 0, 600) / 120;
    if (itemWeight > groupWeight) {
      const { sources, corroborations, sourceCount } = group;
      Object.assign(group, item, { sources, corroborations, sourceCount });
    } else if (!group.signal && item.signal) group.signal = item.signal;
    group.signals = mergedSignals;
    if (!group.signal && mergedSignals.length) group.signal = mergedSignals[0];
    group.tickers = [...new Set([...(group.tickers || []), ...(item.tickers || [])])];
    group.corroborations.push(item);
    if (!group.sources.some((source) => source.name === item.source && source.url === item.url)) {
      group.sources.push({ id: item.sourceId, name: item.source, url: item.url, publishedAt: item.publishedAt, receivedAt: item.receivedAt });
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

function FilterSelect({ label, value, options, onChange }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);
  const selected = options.find((option) => option.value === value) || options[0];

  useEffect(() => {
    if (!open) return undefined;
    const closeOutside = (event) => {
      if (!rootRef.current?.contains(event.target)) setOpen(false);
    };
    const closeOnEscape = (event) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  return (
    <div className={`event-filter-select ${open ? "is-open" : ""}`} ref={rootRef}>
      <button
        type="button"
        className="event-filter-select__trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`${label}: ${selected?.label || ""}`}
        onClick={() => setOpen((current) => !current)}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown") {
            event.preventDefault();
            setOpen(true);
          }
        }}
      >
        <span>{label}</span>
        <strong title={selected?.label}>{selected?.label}</strong>
        <ChevronDown size={13} />
      </button>
      {open && <div className="event-filter-select__menu" role="listbox" aria-label={label}>
        {options.map((option) => (
          <button
            type="button"
            role="option"
            aria-selected={option.value === value}
            className={option.value === value ? "is-selected" : ""}
            key={option.value}
            onClick={() => {
              onChange(option.value);
              setOpen(false);
            }}
          >
            <span>{option.label}</span>
            {option.value === value && <Check size={12} />}
          </button>
        ))}
      </div>}
    </div>
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
  const [hoveredClusterKey, setHoveredClusterKey] = useState(null);

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
  const xForTimestamp = (timestamp) => {
    if (timestamp <= startTime) return 0;
    if (timestamp >= endTime) return plotWidth;
    const rightIndex = candles.findIndex((candle) => new Date(candle.begin).getTime() >= timestamp);
    if (rightIndex <= 0) return 0;
    const leftIndex = rightIndex - 1;
    const leftTime = new Date(candles[leftIndex].begin).getTime();
    const rightTime = new Date(candles[rightIndex].begin).getTime();
    const ratio = rightTime === leftTime ? 0 : (timestamp - leftTime) / (rightTime - leftTime);
    return xForIndex(leftIndex) + (xForIndex(rightIndex) - xForIndex(leftIndex)) * ratio;
  };
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
        x: xForTimestamp(new Date(strongest.publishedAt).getTime()),
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
  const guidedCluster = eventClusters.find((cluster) => cluster.key === hoveredClusterKey) || selectedCluster;
  const guideLabel = guidedCluster ? formatPublicationTime(guidedCluster.strongest.publishedAt) : "";
  const guideWidth = 128;
  const guideX = guidedCluster ? Math.max(2, Math.min(plotWidth - guideWidth - 2, guidedCluster.x - guideWidth / 2)) : 0;

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
          {guidedCluster && <g className="chart-event-guide" aria-hidden="true">
            <line x1={guidedCluster.x} y1={guidedCluster.y + guidedCluster.radius} x2={guidedCluster.x} y2="290" />
            <rect x={guideX} y="291" width={guideWidth} height="18" rx="5" />
            <text x={guideX + guideWidth / 2} y="303.5" textAnchor="middle">{guideLabel}</text>
          </g>}
          {eventClusters.map((cluster) => (
            <g
              className={`chart-event chart-event--${cluster.direction} ${cluster.key === selectedCluster?.key ? "is-selected" : ""}`}
              key={cluster.key}
              role="button"
              tabIndex="0"
              aria-label={`Публикация ${formatPublicationTime(cluster.strongest.publishedAt)}. Открыть ${cluster.newsCount} ${cluster.newsCount === 1 ? "новость" : "новости"}. Самая сильная: ${cluster.strongest.title}`}
              onClick={(clickEvent) => { clickEvent.stopPropagation(); setSelectedClusterKey(cluster.key); setSelectedEventId(cluster.strongest.id); }}
              onPointerEnter={() => setHoveredClusterKey(cluster.key)}
              onPointerLeave={() => setHoveredClusterKey(null)}
              onKeyDown={(keyboardEvent) => { if (keyboardEvent.key === "Enter" || keyboardEvent.key === " ") { setSelectedClusterKey(cluster.key); setSelectedEventId(cluster.strongest.id); } }}
            >
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
        <header><span>{selectedCluster.newsCount > 1 ? `${selectedCluster.newsCount} новости рядом со свечой` : "Публикация новости"}</span><small>Точка на графике = время публикации источником</small></header>
        {selectedCluster.events.map((item, index) => <button type="button" className={`chart-event-detail chart-event-detail--${item.signal?.direction || "neutral"} ${item.id === selectedEvent?.id ? "is-active" : ""}`} key={item.id} onClick={() => { setSelectedEventId(item.id); onOpen?.(item); }}>
          <i />
          <div><span>{index === 0 ? "Главная · формирует сигнал" : item.signal ? "Подтверждает" : "Фон"} · {item.source}{item.signal ? ` · ${formatScore(item.signal.score)} п.` : ""}</span><strong>{item.title}</strong><small className="chart-event-detail__time">Опубликовано {formatPublicationTime(item.publishedAt)}{item.deliveryLagMinutes === null ? "" : ` · EventEdge получил через ${item.deliveryLagMinutes} мин`}</small></div>
          <span className="chart-event-detail__open">Читать <ArrowUpRight size={12} /></span>
        </button>)}
      </div>}
      <div className="chart-legend"><span><i className="legend-price" /> Цена</span><span><i className="legend-volume" /> Объём</span><span><i className="legend-event" /> Точка = время публикации источником; цвет — эффект, размер — вес</span></div>
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

const signalSortOptions = [
  ["priority", "По силе сигнала"],
  ["confidence", "По уверенности"],
  ["freshness", "Сначала свежие"],
  ["ticker", "По тикеру"],
];

function SignalsScreen({ signals, assessmentMeta, marketStatus, marketUpdatedAt, onSelect, onOpenNews, onMethodology, onReadNews }) {
  const [filter, setFilter] = useState("all");
  const [query, setQuery] = useState("");
  const [filterOpen, setFilterOpen] = useState(false);
  const [sortMode, setSortMode] = useState("priority");
  const [sortOpen, setSortOpen] = useState(false);

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
      const availabilityOrder = Number(b.available !== false) - Number(a.available !== false);
      if (availabilityOrder) return availabilityOrder;
      const tickerOrder = a.ticker.localeCompare(b.ticker, "ru-RU");
      if (sortMode === "ticker") return tickerOrder;
      if (sortMode === "freshness") {
        const freshnessOrder = (Date.parse(b.as_of || "") || 0) - (Date.parse(a.as_of || "") || 0);
        return freshnessOrder || tickerOrder;
      }
      const confidenceOrder = Number(b.confidence || 0) - Number(a.confidence || 0);
      const strengthOrder = Math.abs(Number(b.score || 0)) - Math.abs(Number(a.score || 0));
      if (sortMode === "confidence") return confidenceOrder || strengthOrder || tickerOrder;
      return strengthOrder || confidenceOrder || tickerOrder;
    });
  }, [companyCards, filter, query, sortMode]);

  const activeSortLabel = signalSortOptions.find(([value]) => value === sortMode)?.[1] || "Сортировка";

  return (
    <main className="screen screen--signals">
      <section className="terminal-window">
        <div className="terminal-toolbar">
          <div className="terminal-title">
            <div><h1>Компании в фокусе</h1></div>
            <div className="filter-wrap">
              <button type="button" className={`text-button ${filter !== "all" ? "is-active" : ""}`} onClick={() => { setFilterOpen((value) => !value); setSortOpen(false); }}>
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
            <div className="filter-wrap sort-wrap">
              <button className={`sort-button ${sortOpen || sortMode !== "priority" ? "is-active" : ""}`} type="button" aria-label="Выбрать сортировку" aria-expanded={sortOpen} onClick={() => { setSortOpen((value) => !value); setFilterOpen(false); }}>
                <SlidersHorizontal size={15} />
                <span>{activeSortLabel}</span>
                <ChevronDown size={12} />
              </button>
              {sortOpen && (
                <div className="filter-menu sort-menu">
                  {signalSortOptions.map(([value, label]) => (
                    <button key={value} type="button" className={sortMode === value ? "is-active" : ""} onClick={() => { setSortMode(value); setSortOpen(false); }}>
                      {label}
                      {sortMode === value && <Check size={13} />}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>

        <div className="market-strip">
          <span><i className={marketStatus === "error" ? "is-error" : ""} /> MOEX ISS · {marketStatus === "loading" && !marketUpdatedAt ? "загружаем котировки" : marketUpdatedAt ? `обновлено ${formatRelative(marketUpdatedAt)}` : "данные временно недоступны"}</span>
          <span>News engine <strong>signal-engine-0.5.0</strong></span>
          <span>Live assessment <strong>hybrid-market-0.1.0</strong></span>
          <span>Шкала сигнала <strong>от −100 до +100</strong></span>
          <span className="market-strip__right"><strong>{assessmentMeta.directed || 0}</strong> сильных · {assessmentMeta.market_biases || 0} с уклоном · {assessmentMeta.news_backed || 0} с новостью</span>
        </div>

        <div className="company-card-area">
          <div className="company-card-grid">
            {filteredSignals.map((signal) => {
              const available = signal.available !== false;
              const displayDirection = signal.direction;
              const directionLabel = null;
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
                    {available ? <span className="signal-card-status"><Direction direction={displayDirection} label={directionLabel} /><time>Сигнал {formatRelative(signal.signalAt)}</time></span> : <span className="waiting-badge"><i /> Наблюдение</span>}
                  </header>
                  <div className="company-signal-card__body">
                    <div className="company-signal-card__score">
                      <span>{available ? signal.assessment_type === "hybrid" ? "Live оценка компании" : "Рыночный фон" : signal.sector}</span>
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
                    <span><small>{signal.assessment_type === "quant" ? "Оценка рассчитана" : "Сигнал создан"}</small><strong>{formatPublicationTime(signal.signalAt)}</strong></span>
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
        <div className="company-header__signal"><Direction direction={signal.direction} /><span>{signal.horizon} · сигнал {formatPublicationTime(signal.signalAt)}</span></div>
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
            <div className="section-kicker"><CircleGauge size={14} /> {signal.assessment_type === "hybrid" ? "Live оценка · news + market" : "Рыночный фон без свежего news-сигнала"}</div>
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
  const [scope, setScope] = useState("all");
  const [sourceId, setSourceId] = useState("all");
  const [signalState, setSignalState] = useState("all");
  const [direction, setDirection] = useState("all");
  const [period, setPeriod] = useState("7d");
  const [sortMode, setSortMode] = useState("newest");
  const [visibleCount, setVisibleCount] = useState(18);

  useEffect(() => setTicker(initialTicker || "all"), [initialTicker]);
  useEffect(() => setVisibleCount(18), [ticker, query, scope, sourceId, signalState, direction, period, sortMode]);

  const sources = useMemo(() => {
    const result = new Map();
    allNews.forEach((item) => result.set(item.sourceId, item.source));
    return [...result.entries()].sort((left, right) => left[1].localeCompare(right[1], "ru"));
  }, [allNews]);

  const companies = useMemo(() => {
    const tickers = new Set();
    allNews.forEach((item) => (item.tickers || []).forEach((itemTicker) => tickers.add(itemTicker)));
    signals.forEach((signal) => tickers.add(signal.ticker));
    return [...tickers]
      .map((itemTicker) => ({ ticker: itemTicker, company: companyMeta[itemTicker]?.[0] || itemTicker }))
      .sort((left, right) => left.company.localeCompare(right.company, "ru"));
  }, [allNews, signals]);

  const items = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase("ru-RU");
    const periodDays = { "1d": 1, "3d": 3, "7d": 7, "30d": 30 }[period];
    const cutoff = periodDays ? Date.now() - periodDays * 86400000 : null;
    return allNews.filter((item) => {
      const matchesTicker = ticker === "all" || item.tickers?.includes(ticker);
      const matchesScope = scope === "all" || item.event?.scope === scope;
      const matchesSource = sourceId === "all" || item.sources?.some((source) => source.id === sourceId) || item.sourceId === sourceId;
      const matchesSignal = signalState === "all" || (signalState === "signal" ? item.signals?.length > 0 : !item.signals?.length);
      const matchesDirection = direction === "all" || item.signals?.some((signal) => signal.direction === direction);
      const matchesPeriod = cutoff === null || new Date(item.publishedAt).getTime() >= cutoff;
      const haystack = `${item.title} ${item.content} ${item.source} ${(item.tickers || []).join(" ")} ${(item.event?.sectors || []).join(" ")} ${(item.signals || []).map((signal) => signal.target?.label || signal.company || signal.ticker).join(" ")} ${item.companySignal?.company || ""}`.toLocaleLowerCase("ru-RU");
      return matchesTicker && matchesScope && matchesSource && matchesSignal && matchesDirection && matchesPeriod && (!normalized || haystack.includes(normalized));
    }).sort((left, right) => {
      if (sortMode === "impact") {
        const impact = Math.max(0, ...(right.signals || []).map((signal) => Math.abs(signal.score || 0))) - Math.max(0, ...(left.signals || []).map((signal) => Math.abs(signal.score || 0)));
        if (impact) return impact;
      }
      return new Date(right.publishedAt) - new Date(left.publishedAt);
    });
  }, [allNews, ticker, query, scope, sourceId, signalState, direction, period, sortMode]);

  const scopeCounts = useMemo(() => allNews.reduce((result, item) => {
    const key = item.event?.scope || "market";
    result[key] = (result[key] || 0) + 1;
    return result;
  }, {}), [allNews]);
  const visibleItems = items.slice(0, visibleCount);
  const processingStats = useMemo(() => {
    const apiStats = newsMeta.processing_coverage;
    if (apiStats) return {
      relevant: apiStats.relevant,
      analyzed: apiStats.analysis_candidates,
      withSignal: apiStats.signaled,
      coverage: Math.round(apiStats.candidate_coverage_pct || 0),
      modelVersion: apiStats.signal_model_version,
    };
    const relevant = allNews.filter((item) => item.processing?.event_candidate || item.processing?.analysis_candidate);
    const analyzed = relevant.filter((item) => item.processing?.analysis_candidate);
    const withSignal = allNews.filter((item) => item.signals?.length).length;
    return {
      relevant: relevant.length,
      analyzed: analyzed.length,
      withSignal,
      coverage: relevant.length ? Math.round(analyzed.length / relevant.length * 100) : 0,
      modelVersion: "signal-engine-0.5.0",
    };
  }, [allNews, newsMeta.processing_coverage]);
  const activeFilterCount = [ticker, scope, sourceId, signalState, direction, period, sortMode]
    .filter((value, index) => value !== ["all", "all", "all", "all", "all", "7d", "newest"][index]).length + (query ? 1 : 0);
  const resetFilters = () => {
    setTicker("all"); setScope("all"); setSourceId("all"); setSignalState("all");
    setDirection("all"); setPeriod("7d"); setSortMode("newest"); setQuery("");
  };

  return (
    <main className="screen section-screen news-screen">
      <section className="page-hero news-hero">
        <div><span className="eyebrow"><Newspaper size={13} /> Карта событий</span><h1>Не лента, а структура рынка</h1><p>EventEdge сохраняет каждую публикацию, выделяет экономически значимые события и независимо рассчитывает сигнал для компании, отрасли или российского рынка.</p></div>
        <label className="page-search"><Search size={15} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Компания, тикер или событие" />{query && <button type="button" onClick={() => setQuery("")} aria-label="Очистить поиск"><X size={13} /></button>}</label>
      </section>

      <section className="event-scope-overview">
        {[{ key: "all", label: "Все новости", Icon: Layers3 }, ...["market", "sector", "company"].map((key) => ({ key, ...eventScopeMeta[key] }))].map((meta) => {
          const key = meta.key;
          const Icon = meta.Icon;
          const count = key === "all" ? allNews.length : scopeCounts[key] || 0;
          return <button type="button" key={key} className={scope === key ? "is-active" : ""} onClick={() => setScope(key)}><Icon size={15} /><span><strong>{meta.label}</strong><small>{count} публикаций</small></span></button>;
        })}
      </section>

      <section className="news-controlbar">
        <div className="event-selects event-selects--news">
          <FilterSelect label="Компания" value={ticker} onChange={setTicker} options={[{ value: "all", label: "Все бумаги" }, ...companies.map((item) => ({ value: item.ticker, label: `${item.ticker} · ${item.company}` }))]} />
          <FilterSelect label="Источник" value={sourceId} onChange={setSourceId} options={[{ value: "all", label: "Все источники" }, ...sources.map(([id, name]) => ({ value: id, label: name }))]} />
          <FilterSelect label="Период" value={period} onChange={setPeriod} options={[{ value: "1d", label: "24 часа" }, { value: "3d", label: "3 дня" }, { value: "7d", label: "7 дней" }, { value: "30d", label: "30 дней" }, { value: "all", label: "Всё время" }]} />
          <FilterSelect label="Сигнал" value={signalState} onChange={setSignalState} options={[{ value: "all", label: "Все" }, { value: "signal", label: "Есть сигнал" }, { value: "context", label: "Только контекст" }]} />
          <FilterSelect label="Направление" value={direction} onChange={setDirection} options={[{ value: "all", label: "Любое" }, { value: "up", label: "Вверх" }, { value: "neutral", label: "Нейтрально" }, { value: "down", label: "Вниз" }]} />
          <FilterSelect label="Сортировка" value={sortMode} onChange={setSortMode} options={[{ value: "newest", label: "Сначала новые" }, { value: "impact", label: "Сначала весомые" }]} />
          {activeFilterCount > 0 && <button type="button" className="signal-filter is-active" onClick={resetFilters}><X size={13} /> Сбросить · {activeFilterCount}</button>}
        </div>
      </section>

      <div className="pipeline-status"><span><i /> Быстрый сбор работает</span><strong>Приоритетные источники проверяются каждую минуту</strong><small>{newsMeta.last_ingested_at ? `Последняя новая запись ${formatRelative(newsMeta.last_ingested_at)} · цель доставки до ${Math.round((newsMeta.delivery_target_seconds || 120) / 60)} мин` : "Ожидаем первую публикацию"}</small></div>
      <section className="coverage-strip" aria-label="Покрытие обработки новостей">
        <article><span>Экономически релевантные</span><strong>{processingStats.relevant}</strong><small>рынок · отрасли · компании</small></article>
        <article><span>Переданы в Signal Engine</span><strong>{processingStats.coverage}%</strong><small>{processingStats.analyzed} из {processingStats.relevant} · {processingStats.modelVersion}</small></article>
        <article><span>Публикации с сигналом</span><strong>{processingStats.withSignal}</strong><small>включая отраслевые и рыночные</small></article>
        <p><Info size={13} /> Исторический backfill идёт небольшими пакетами; покрытие растёт без удаления старых версий сигналов.</p>
      </section>
      <section className="news-feed">
        <div className="feed-heading"><span>{items.length} из {allNews.length} публикаций</span><small>{newsMeta.sources?.length || sources.length} источников · повторы одного события объединяются</small></div>
        {visibleItems.map((item) => {
          const scopeMeta = eventScopeMeta[item.event?.scope || "market"];
          const ScopeIcon = scopeMeta.Icon;
          return (
          <button type="button" className={`feed-item event-feed-item event-feed-item--${item.event?.scope || "market"}`} key={item.id} onClick={() => onReadNews(item)}>
            {item.signal?.target?.type === "instrument" || item.companySignal ? <CompanyMark signal={item.signal?.target?.type === "instrument" ? item.signal : item.companySignal} /> : <span className={`company-mark company-mark--${item.event?.scope || "market"}`}><ScopeIcon size={17} /></span>}
            <div className="feed-copy"><div><span className={`event-scope event-scope--${item.event?.scope || "market"}`}><ScopeIcon size={10} /> {scopeMeta.label}</span><span>{item.source}</span>{item.sourceCount > 1 && <i className="source-count">{item.sourceCount} источника</i>}<time>Опубликовано {formatPublicationTime(item.publishedAt)}</time></div><h2>{item.title}</h2><p>{item.content}</p><footer>{item.event?.sectors?.map((sector) => <span className="event-sector" key={sector}>{sector}</span>)}{item.signals?.length ? <span className="event-signal-list">{item.signals.map((signal) => <span className={`event-signal-chip event-signal-chip--${signal.direction}`} key={signal.id}><strong>{signal.target?.label || signal.ticker}</strong><Direction direction={signal.direction} /><span>{formatScore(signal.score)} п.</span></span>)}</span> : <span className="context-only">Сохранено · сигнал не рассчитан</span>}</footer></div>
            <BookOpen size={17} />
          </button>
        );})}
        {!items.length && <div className="empty-state"><Search size={22} /><strong>Новостей не найдено</strong><span>Измени запрос или выбери другую компанию.</span></div>}
        {visibleCount < items.length && <button type="button" className="show-more-events" onClick={() => setVisibleCount((value) => value + 18)}>Показать ещё {Math.min(18, items.length - visibleCount)} событий <ArrowDownRight size={13} /></button>}
      </section>
    </main>
  );
}

function EvalsScreen() {
  const [payload, setPayload] = useState(null);
  const [status, setStatus] = useState("loading");
  const [error, setError] = useState("");
  const [selectedModelVersion, setSelectedModelVersion] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    let refreshing = false;
    const load = async () => {
      if (refreshing || document.visibilityState === "hidden") return;
      refreshing = true;
      try {
        const query = selectedModelVersion ? `?model_version=${encodeURIComponent(selectedModelVersion)}` : "";
        const response = await fetch(apiUrl(`/v1/evals${query}`), { signal: controller.signal });
        if (!response.ok) throw new Error("Evals API временно недоступен.");
        setPayload(await response.json());
        setStatus("ready");
        setError("");
      } catch (loadError) {
        if (loadError.name !== "AbortError") {
          setError(loadError.message || "Не удалось загрузить Evals.");
          setPayload((current) => {
            setStatus(current ? "ready" : "error");
            return current;
          });
        }
      } finally {
        refreshing = false;
      }
    };
    load();
    const interval = window.setInterval(load, 300000);
    const handleVisibility = () => { if (document.visibilityState === "visible") load(); };
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      controller.abort();
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [selectedModelVersion]);

  if (status !== "ready") return <DataState error={status === "error" ? error : ""} onRetry={() => window.location.reload()} />;

  const { summary, breakdowns, relationships, quality_series: qualitySeries, outcomes } = payload.data;
  const hitRate = summary.hit_rate_pct === null ? "—" : `${summary.hit_rate_pct}%`;
  const averageReturn = summary.average_signed_return_pct === null ? "—" : formatPct(summary.average_signed_return_pct);
  const medianReturn = summary.median_signed_return_pct === null ? "—" : formatPct(summary.median_signed_return_pct);
  const horizonLabels = { "1h": "1 час", "4h": "4 часа", "1d": "1 день", "3d": "3 дня" };
  const directionLabels = { up: "Вверх", down: "Вниз", neutral: "Нейтрально" };
  const latestQuality = qualitySeries.at(-1);
  const modelEpochs = payload.meta.model_epochs || [];
  const activeModelVersion = payload.meta.selected_model_version || selectedModelVersion;

  return (
    <main className="screen section-screen evals-screen">
      <section className="page-hero evals-hero">
        <div><span className="eyebrow"><Activity size={13} /> Проверка реальностью</span><h1>Evals: сигналы в цифрах</h1><p>Только направленные сигналы «вверх» и «вниз» сопоставляются с реальной ценой MOEX. Нейтральные события сохраняются в истории, но не искажают hit rate.</p></div>
        <div className="eval-hero-actions">
          <div className={`eval-live${error ? " is-stale" : ""}`}><i /><span><strong>{error ? "Показываем последний snapshot" : "Snapshot каждые 10 минут"}</strong><small>{payload.meta.generated_at ? `${formatRelative(payload.meta.generated_at)} · ` : "Расчёт новой эпохи ожидается · "}основной горизонт 4 часа</small></span></div>
          <label className="eval-model-select"><span>Эпоха модели</span><select value={activeModelVersion || ""} onChange={(event) => setSelectedModelVersion(event.target.value)}>{modelEpochs.map((epoch) => <option value={epoch.model_version} key={epoch.epoch_id}>{epoch.model_version} · cfg {epoch.config_version} · n={epoch.signals}</option>)}</select><ChevronDown size={13} /></label>
          <div className="eval-exports">
            <a href={apiUrl("/v1/evals/export?format=csv&dataset=outcomes&model_version=all")} download><Download size={13} /> Все эпохи · outcomes</a>
            <a href={apiUrl("/v1/evals/export?format=csv&dataset=timeseries&model_version=all")} download><Download size={13} /> Все эпохи · raw</a>
          </div>
        </div>
      </section>

      <section className="eval-warning"><ShieldCheck size={17} /><div><strong>Это технический eval, а не доказательство доходности</strong><span>Выборка пока мала и не является point‑in‑time калиброванным backtest. Результаты нужны, чтобы находить слабые места модели до использования капитала.</span></div></section>

      <section className="eval-kpis">
        <article><span>Проверено сигналов</span><strong>{summary.evaluated}</strong><small>из {summary.signals_total} доступных в хранилище</small></article>
        <article><span>Попадание направления</span><strong>{hitRate}</strong><small>через 4 часа, fallback на 1 час</small></article>
        <article><span>Средняя реакция</span><strong className={Number(summary.average_signed_return_pct) >= 0 ? "market-positive" : "market-negative"}>{averageReturn}</strong><small>медиана {medianReturn} · signed return за 4 часа</small></article>
        <article><span>Покрытие eval</span><strong>{summary.coverage_pct}%</strong><small>{summary.pending} ждут первый outcome · {summary.partial || 0} оценены частично · {summary.unavailable} вне окна</small></article>
      </section>

      <section className="eval-analysis-grid">
        <article className="eval-analysis-card">
          <div className="section-heading"><span><Clock3 size={14} /> По горизонтам</span><small>где сигнал работает лучше</small></div>
          <div className="eval-metric-rows">
            {breakdowns.by_horizon.map((item) => <div key={item.horizon}>
              <strong>{horizonLabels[item.horizon]}</strong>
              <span>{item.hit_rate_pct === null ? "—" : `${item.hit_rate_pct}%`}<small>hit rate</small></span>
              <span className={Number(item.average_signed_return_pct) >= 0 ? "market-positive" : "market-negative"}>{formatPct(item.average_signed_return_pct)}<small>среднее</small></span>
              <em>n={item.observations}</em>
            </div>)}
          </div>
        </article>

        <article className="eval-analysis-card">
          <div className="section-heading"><span><CircleGauge size={14} /> Что связано с качеством</span><small>Pearson · не причинность</small></div>
          <div className="relationship-list">
            {relationships.map((item) => <div key={item.code}>
              <span><strong>{item.label}</strong><small>{item.interpretation} · n={item.observations}</small></span>
              <b>{item.value === null ? "r —" : `r ${item.value > 0 ? "+" : ""}${item.value.toFixed(2)}`}</b>
            </div>)}
          </div>
          <p>Корреляция станет содержательной после накопления выборки. Сейчас «нет данных» лучше ложной точности.</p>
        </article>

        <article className="eval-analysis-card">
          <div className="section-heading"><span><ArrowUpRight size={14} /> По направлениям</span><small>асимметрия модели</small></div>
          <div className="eval-direction-grid">
            {breakdowns.by_direction.map((item) => <div key={item.direction}>
              <Direction direction={item.direction} />
              <strong>{directionLabels[item.direction]}</strong>
              <b>{item.hit_rate_pct === null ? "—" : `${item.hit_rate_pct}%`}</b>
              <small>{item.observations} оценок · {formatPct(item.average_signed_return_pct)}</small>
            </div>)}
          </div>
          <div className="confidence-strip">
            {breakdowns.by_confidence.map((item) => <div key={item.bucket}><strong>{item.bucket}</strong><span>{item.hit_rate_pct === null ? "—" : `${item.hit_rate_pct}%`}</span><small>n={item.observations}</small></div>)}
          </div>
        </article>

        <article className="eval-analysis-card">
          <div className="section-heading"><span><BarChart3 size={14} /> Накопленное качество</span><small>{latestQuality ? `${latestQuality.evaluated_count} решённых сигналов` : "нет наблюдений"}</small></div>
          <div className="quality-series">
            {qualitySeries.slice(-16).map((point) => <div key={point.signal_id} title={`${point.ticker}: ${point.cumulative_hit_rate_pct}%`}>
              <i style={{ height: `${Math.max(6, point.cumulative_hit_rate_pct)}%` }} />
              <span>{point.ticker}</span>
            </div>)}
            {!qualitySeries.length && <p>Линия появится после первых сигналов с доступным outcome.</p>}
          </div>
          {latestQuality && <div className="quality-latest"><span>Текущий cumulative hit rate</span><strong>{latestQuality.cumulative_hit_rate_pct}%</strong><small>средний signed return {formatPct(latestQuality.cumulative_average_signed_return_pct)}</small></div>}
        </article>
      </section>

      <section className="eval-ticker-card">
        <div className="section-heading"><span><Database size={14} /> Разрез по бумагам</span><small>для поиска систематических ошибок</small></div>
        <div className="ticker-eval-grid">
          {breakdowns.by_ticker.slice(0, 12).map((item) => <div key={item.ticker}><strong>{item.ticker}</strong><span>{item.hit_rate_pct === null ? "—" : `${item.hit_rate_pct}%`}<small>hit rate</small></span><span>{formatPct(item.average_signed_return_pct)}<small>avg signed</small></span><em>n={item.observations}</em></div>)}
        </div>
        {!breakdowns.by_ticker.length && <div className="empty-state"><Database size={22} /><strong>Данные ещё не накопились</strong><span>Разрезы появятся автоматически.</span></div>}
      </section>

      <section className="eval-export-note">
        <FileText size={17} />
        <div><strong>Данные по эпохам не затираются</strong><span>UI считает качество на 1–4 часах. В БД и выгрузке хранятся model/config version и сырые 10‑минутные свечи до +3 дней для временных рядов.</span></div>
        <a href={apiUrl("/v1/evals/export?format=json&dataset=outcomes&model_version=all")} target="_blank" rel="noreferrer">Все эпохи JSON <ArrowUpRight size={12} /></a>
      </section>

      <section className="outcomes-card">
        <div className="section-heading"><span><BarChart3 size={15} /> Реакция после каждого сигнала</span><small>цена от первой торгуемой свечи</small></div>
        <div className="eval-table-wrap">
          <table className="eval-table outcomes-table">
            <colgroup><col className="outcome-col-signal" /><col className="outcome-col-news" /><col span="4" className="outcome-col-return" /><col className="outcome-col-verdict" /></colgroup>
            <thead><tr><th>Сигнал</th><th>Новость</th><th>1 час</th><th>4 часа</th><th>1 день</th><th>3 дня</th><th>Вердикт</th></tr></thead>
            <tbody>
              {outcomes.slice(0, 30).map((outcome) => {
                const observedHorizon = ["4h", "1h"].find((period) => outcome.returns?.[period] !== null && outcome.returns?.[period] !== undefined);
                const observedLabel = observedHorizon ? horizonLabels[observedHorizon] : null;
                const verdict = outcome.status === "unavailable"
                  ? <span className="eval-verdict is-unavailable">Нет истории</span>
                  : outcome.verdict === null
                    ? <span className="eval-verdict is-pending">Ждём 1 час</span>
                    : outcome.verdict
                      ? <span className="eval-verdict is-hit"><Check size={11} /> {outcome.status === "partial" ? `Пока попал · ${observedLabel}` : "Попал"}</span>
                      : <span className="eval-verdict is-miss"><X size={11} /> {outcome.status === "partial" ? `Пока не попал · ${observedLabel}` : "Не попал"}</span>;
                return <tr key={outcome.signal_id}>
                  <td><div className="outcome-signal"><strong>{outcome.ticker}</strong><Direction direction={outcome.direction} /><small>{formatScore(outcome.score)} п.</small></div></td>
                  <td>{outcome.news ? <a href={outcome.news.url} target="_blank" rel="noreferrer" title={outcome.news.title}><span>{outcome.news.source_id}</span><strong>{outcome.news.title}</strong></a> : <span>Источник недоступен</span>}</td>
                  {["1h", "4h", "1d", "3d"].map((period) => {
                    const value = outcome.returns?.[period];
                    return <td key={period} className={value === null || value === undefined ? "" : Number(value) >= 0 ? "market-positive" : "market-negative"}>{formatPct(value)}</td>;
                  })}
                  <td>{verdict}</td>
                </tr>;
              })}
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
  const [sourceRegistry, setSourceRegistry] = useState(readCachedSourceRegistry);
  const [sourceRegistryStatus, setSourceRegistryStatus] = useState(sourceRegistry ? "cached" : "loading");
  const [showSourceForm, setShowSourceForm] = useState(false);
  const [channel, setChannel] = useState("");
  const [channelName, setChannelName] = useState("");
  const [channelDescription, setChannelDescription] = useState("");
  const [adminKey, setAdminKey] = useState("");
  const [sourceFormStatus, setSourceFormStatus] = useState("idle");
  const [sourceFormMessage, setSourceFormMessage] = useState("");
  const recentBySource = useMemo(() => {
    const result = new Map();
    allNews.forEach((item) => {
      (item.sources || [{ id: item.sourceId, publishedAt: item.publishedAt }]).forEach((source) => {
        if (!result.has(source.id)) result.set(source.id, source.publishedAt);
      });
    });
    return result;
  }, [allNews]);

  const loadSources = async () => {
    try {
      const response = await fetch(apiUrl("/v1/sources"));
      if (!response.ok) throw new Error("Реестр источников временно недоступен.");
      const payload = await response.json();
      setSourceRegistry(payload);
      cacheSourceRegistry(payload);
      setSourceRegistryStatus("ready");
    } catch {
      setSourceRegistry((current) => current || readCachedSourceRegistry());
      setSourceRegistryStatus("error");
    }
  };

  useEffect(() => { loadSources(); }, []);

  const sources = sourceRegistry?.data?.map((source) => ({
    id: source.source_id,
    name: source.name,
    kind: source.kind,
    quality: source.quality,
    freshness: source.freshness,
    role: source.role || "Публичный Telegram-канал",
    url: source.url,
    count: source.count,
    lastPublishedAt: source.last_published_at,
    managed: source.managed,
  })) || methodologySources;

  const addTelegramSource = async (event) => {
    event.preventDefault();
    setSourceFormStatus("saving");
    setSourceFormMessage("");
    try {
      const response = await fetch(apiUrl("/v1/sources/telegram"), {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-EventEdge-Admin-Key": adminKey },
        body: JSON.stringify({
          channel,
          display_name: channelName || undefined,
          description: channelDescription || undefined,
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || payload.title || "Не удалось добавить канал.");
      setChannel("");
      setChannelName("");
      setChannelDescription("");
      setAdminKey("");
      setSourceFormStatus("saved");
      setSourceFormMessage("Канал добавлен. Первый опрос — в течение 5 минут.");
      await loadSources();
    } catch (error) {
      setSourceFormStatus("error");
      setSourceFormMessage(error.message || "Не удалось добавить канал.");
    }
  };

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

      <section className="model-stack">
        <article><span>01</span><div><strong>signal-engine-0.5.0</strong><small>Новостной сигнал</small><p>LLM извлекает событие, факты, полярность и существенность. Код выбирает target — компания, отрасль или рынок — и рассчитывает score.</p></div></article>
        <i><ArrowDownRight size={15} /></i>
        <article><span>02</span><div><strong>hybrid-market-0.1.0</strong><small>Live оценка компании</small><p>Для конкретной акции объединяет активный news-сигнал с ценой, объёмом, волатильностью, ликвидностью и доступной отчётностью.</p></div></article>
        <i><ArrowDownRight size={15} /></i>
        <article><span>03</span><div><strong>Evals по эпохам</strong><small>Проверка после сигнала</small><p>Сохраняет результаты каждой версии отдельно и оценивает реакцию через 1 и 4 часа; сырые точки до 3 дней остаются в выгрузке.</p></div></article>
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
        <header><span>04</span><div><h2>Не‑LLM слой проверяет рынок</h2><p>Без направленной новости факторы показываются как рыночный уклон, но не превращаются в зелёный или красный торговый сигнал. Если сильная новость есть, итог считается по фиксированной пропорции 65% news / 35% market.</p></div></header>
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

      <section className="event-method">
        <header><span>06</span><div><h2>Каждая публикация становится событием</h2><p>Это три независимых масштаба контекста, а не обязательная цепочка влияния.</p></div></header>
        <div className="event-method__grid">
          <article><Globe2 size={16} /><strong>Рынок</strong><span>Независимый risk-on / risk-off сигнал по общему фактору — без автоматического назначения всем акциям.</span></article>
          <article><Layers3 size={16} /><strong>Отрасль</strong><span>Независимый сигнал по затронутому сектору, например сельскому хозяйству или логистике.</span></article>
          <article><BarChart3 size={16} /><strong>Компания</strong><span>Сигнал конкретной бумаге возникает только при доказанной прямой связи публикации с эмитентом.</span></article>
        </div>
        <p><Info size={14} /> Это не обязательная цепочка «рынок → отрасль → компания». Одна публикация получает тот target, для которого эффект обоснован; распространение на отдельные бумаги требует отдельной калибровки.</p>
      </section>

      <section className="source-method">
        <div className="section-heading"><span><Database size={15} /> Источники и их роль</span><button type="button" onClick={() => setShowSourceForm((value) => !value)}><Plus size={13} /> Добавить Telegram</button></div>
        {sourceRegistryStatus === "error" && <div className="source-registry-warning"><Server size={14} /><span><strong>Live-реестр временно недоступен</strong>Показываем последний сохранённый список. Каналы в YDB не удалены.</span></div>}
        {sourceRegistryStatus === "cached" && <div className="source-registry-warning is-cached"><Database size={14} /><span><strong>Показываем сохранённый реестр</strong>Проверяем актуальное состояние в фоне.</span></div>}
        {showSourceForm && <form className="telegram-source-form" onSubmit={addTelegramSource}>
          <div><strong>Новый публичный Telegram-канал</strong><span>EventEdge читает публичную web-ленту без бота. Лимит — {sourceRegistry?.meta?.telegram_limit || 18} каналов, пользовательские каналы опрашиваются раз в 5 минут.</span></div>
          <label><span>Канал</span><input required pattern="@?[A-Za-z0-9_]{3,48}" value={channel} onChange={(event) => setChannel(event.target.value)} placeholder="@channel_name" /></label>
          <label><span>Название</span><input value={channelName} onChange={(event) => setChannelName(event.target.value)} placeholder="Как показывать в EventEdge" /></label>
          <label className="source-description-field"><span>Описание</span><input minLength="8" maxLength="280" value={channelDescription} onChange={(event) => setChannelDescription(event.target.value)} placeholder="Что публикует источник и зачем он нужен" /></label>
          <label><span>Админ-ключ</span><input required type="password" autoComplete="off" value={adminKey} onChange={(event) => setAdminKey(event.target.value)} placeholder="Не сохраняется в браузере" /></label>
          <button type="submit" disabled={sourceFormStatus === "saving"}>{sourceFormStatus === "saving" ? "Добавляем…" : "Добавить канал"}</button>
          {sourceFormMessage && <p className={`source-form-message is-${sourceFormStatus}`}>{sourceFormMessage}</p>}
        </form>}
        <div className="source-method__grid">
          {sources.map((source) => {
            const lastSeen = source.lastPublishedAt || recentBySource.get(source.id);
            const sourceStat = source.count !== undefined ? { count: source.count } : newsMeta.sources?.find((item) => item.source_id === source.id);
            const hasData = source.id === "moex_iss" || Boolean(lastSeen);
            return (
              <a href={source.url} target="_blank" rel="noreferrer" key={source.id} className="source-card">
                <header><span>{source.kind}{source.managed ? " · UI" : ""}</span><strong>{source.quality}/100</strong></header>
                <h3>{source.name}<ArrowUpRight size={12} /></h3>
                <p>{source.role}</p>
                <footer><span className={hasData ? "is-live" : "is-waiting"}><i /> {hasData ? sourceStat ? `${sourceStat.count} публикаций` : "Есть данные" : "Подключён · без событий"}</span><time>{lastSeen ? formatRelative(lastSeen) : source.freshness}</time></footer>
              </a>
            );
          })}
        </div>
        <p className="source-note">Все Telegram- и RSS-публикации сохраняются. High-recall фильтр отправляет в LLM только экономически релевантные события; компания, отрасль или рынок выбираются кодом после семантического разбора. Качество пользовательского канала по умолчанию — 65/100.</p>
      </section>

      <section className="method-reality">
        <div><ShieldCheck size={17} /><span><strong>Что работает сейчас</strong>Signal Engine 0.5, независимые market/sector/company targets, пять не‑LLM факторов, live‑оценка бумаг, outcomes и выгрузки по эпохам.</span></div>
        <div><Database size={17} /><span><strong>Граница текущей версии</strong>Отчётность пока извлекается из распознанных раскрытий; полноценный point‑in‑time фундаментальный датасет и калиброванный backtest ещё не готовы.</span></div>
        <button type="button" onClick={onApi}>Посмотреть API <ArrowUpRight size={13} /></button>
      </section>
    </main>
  );
}

const apiEndpoints = [
  { id: "assessments", method: "GET", path: "/v1/assessments", title: "Live‑оценки рынка", description: "Гибридная оценка направленных news-сигналов и отдельный quant-уклон для остальных компаний.", parameter: { name: "tickers", type: "string", description: "Опциональный список тикеров MOEX через запятую" } },
  { id: "evals", method: "GET", path: "/v1/evals", title: "Анализ качества сигналов", description: "Короткие 1ч/4ч метрики, Pearson и выбор сохранённой эпохи модели.", parameter: { name: "model_version", type: "string", description: "Версия news-модели; без параметра выбирается текущая эпоха" } },
  { id: "evals_export", method: "GET", path: "/v1/evals/export?format=csv&dataset=timeseries&model_version=all", title: "Выгрузка Evals", description: "CSV/JSON: все эпохи моделей и сырой event-time ряд свечей до +3 дней.", parameter: { name: "dataset", type: "string", description: "outcomes или timeseries; model_version — версия или all" } },
  { id: "signals", method: "GET", path: "/v1/signals?limit=20", title: "Сигналы Signal Engine", description: "Версия signal-engine-0.5.0: target может быть инструментом, отраслью или рынком. Live-оценка конкретной компании возвращается отдельно через /v1/assessments.", parameter: { name: "limit", type: "integer", description: "Количество записей, максимум 100" } },
  { id: "news", method: "GET", path: "/v1/news?limit=20", title: "Лента новостей", description: "Исходные публикации, event-проекция и связанные сигналы.", parameter: { name: "limit", type: "integer", description: "Количество публикаций, максимум 500" } },
  { id: "events", method: "GET", path: "/v1/events?limit=20", title: "Рыночные события", description: "Публикации как события уровня рынок, отрасль или компания.", parameter: { name: "scope", type: "string", description: "market, sector или company" } },
  { id: "sources", method: "GET", path: "/v1/sources", title: "Реестр источников", description: "Подключённые RSS и Telegram-источники, свежесть и статистика сбора.", parameter: null },
  { id: "source_create", method: "POST", path: "/v1/sources/telegram", title: "Добавить Telegram", description: "Защищённое добавление публичного канала в пятиминутный контур.", parameter: { name: "X-EventEdge-Admin-Key", type: "header", description: "Админ-ключ runtime; в UI не сохраняется" } },
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
    "breakdowns": {"by_horizon":[{"horizon":"4h","hit_rate_pct":58.3}]},
    "relationships": [{"code":"signal_strength_vs_4h_return","value":0.21}],
    "outcomes": [{"ticker":"SBER","model_version":"signal-engine-0.5.0","returns":{"1h":0.4,"4h":0.8,"1d":1.2,"3d":2.1},"verdict":true}]
  },
  "meta": {"selected_model_version":"signal-engine-0.5.0","primary_horizon":"4h","evaluation_scope":"directional_signals_only"}
}` : endpoint.id === "evals_export" ? `signal_id,ticker,signal_as_of,direction,score,confidence,model_version,config_version,observation_at,offset_minutes,return_pct
sig_01,SBER,2026-08-08T07:00:00Z,up,42.7,0.76,signal-engine-0.5.0,1,2026-08-08T08:00:00Z,60,0.42` : endpoint.id === "snapshot" ? `{
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
}` : endpoint.id === "sources" ? `{
  "data": [
    {"source_id":"telegram_bcs_express","name":"БКС Экспресс","kind":"Telegram","count":12,"freshness":"цель ≤ 2 мин"}
  ],
  "meta": {"telegram_active":18,"telegram_limit":18,"poll_interval_seconds":60,"managed_telegram_poll_interval_seconds":300}
}` : endpoint.id === "source_create" ? `{
  "data": {"source_id":"telegram_example","channel":"example","enabled":true,"managed":true}
}` : endpoint.id === "events" ? `{
  "data": [{
    "id":"event_…","scope":"sector","scope_label":"Отрасль","sectors":["Ритейл и логистика"],
    "title":"Событие затронуло склад маркетплейса","related_signals":[]
  }],
  "meta":{"limit":20,"scope":null}
}` : endpoint.id === "news" ? `{
  "data": [{
    "id": "news_…",
    "source_id": "moex_news",
    "title": "Сообщение эмитента",
    "url": "https://www.moex.com/n…",
    "related_signals": [{"ticker":"RUAGRI","target":{"type":"sector","id":"AGRICULTURE","label":"Сельское хозяйство"},"direction":"up","score":46.7}]
  }],
  "meta": {
    "limit":20,
    "total":84,
    "has_more":true,
    "poll_interval_seconds":60,
    "client_refresh_interval_seconds":30,
    "delivery_target_seconds":120,
    "processing_coverage":{"stored":84,"relevant":61,"analysis_candidates":54,"signaled":27,"candidate_coverage_pct":88.5,"signal_yield_pct":50.0,"signal_model_version":"signal-engine-0.5.0"},
    "collection_lanes":[{"id":"fast","interval_seconds":60,"source_ids":["interfax","tass","rbc","moex_news"]}],
    "sources":[{"source_id":"interfax","count":24,"signal_count":5}]
  }
}` : `{
  "data": [
    {
      "id": "sig_01JZK6K5GDX90Q2X8C0R4D7M9P",
      "ticker": "RUAGRI",
      "target": {"type":"sector","id":"AGRICULTURE","label":"Сельское хозяйство"},
      "direction": "up",
      "score": 42.7,
      "confidence": 0.76,
      "horizon": {"value": 3, "unit": "calendar_days"},
      "model_version": "signal-engine-0.5.0"
    }
  ],
  "meta": {
    "limit": 20,
    "has_more": false,
    "next_cursor": null,
    "model_scope": "news_event",
    "final_assessment_endpoint": "/v1/assessments",
    "final_assessment_model_version": "hybrid-market-0.1.0"
  }
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
  const curlExample = endpoint.id === "source_create"
    ? `curl -s -X POST '${baseUrl}${endpoint.path}' \\
  -H 'Content-Type: application/json' \\
  -H 'X-EventEdge-Admin-Key: <ADMIN_KEY>' \\
  -d '{"channel":"example_channel","display_name":"Example","description":"Оперативные новости российского рынка"}'`
    : `curl -s '${baseUrl}${endpoint.path}' \\
  -H 'Accept: application/json'`;

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
          <div className="code-panel"><div><span><Terminal size={13} /> cURL</span><button type="button" onClick={() => copyText(curlExample, "curl")}><Copy size={13} /> {copied === "curl" ? "Готово" : "Копировать"}</button></div><pre><code>{curlExample}</code></pre></div>
          <div className="response-panel"><span>Пример ответа</span><pre><code>{responseExample}</code></pre></div>
        </section>
      </div>
    </main>
  );
}

function NewsReader({ item, onClose }) {
  if (!item) return null;
  const relatedSignals = item.signals || (item.signal ? [item.signal] : []);
  const companySignal = relatedSignals.find((signal) => signal.target?.type === "instrument") || item.companySignal;
  const scopeMeta = eventScopeMeta[item.event?.scope || "market"];
  const ScopeIcon = scopeMeta.Icon;
  const signalCountText = relatedSignals.length === 1
    ? "одну независимую оценку"
    : relatedSignals.length < 5
      ? `${relatedSignals.length} независимые оценки`
      : `${relatedSignals.length} независимых оценок`;
  return (
    <div className="reader-overlay">
      <button className="overlay-dismiss" type="button" aria-label="Закрыть новость" onClick={onClose} />
      <article className="reader-dialog" role="dialog" aria-modal="true" aria-label="Просмотр новости">
        <header><div>{companySignal ? <CompanyMark signal={companySignal} /> : <span className={`company-mark company-mark--${item.event?.scope || "market"}`}><ScopeIcon size={17} /></span>}<span><strong>{companySignal?.ticker || relatedSignals[0]?.target?.label || "Новость"}</strong><small>{companySignal ? `${companySignal.company} · ` : ""}{item.source}</small></span></div><button type="button" onClick={onClose} aria-label="Закрыть"><X size={18} /></button></header>
        <div className="reader-meta"><span>{scopeMeta.label}{item.event?.sectors?.length ? ` · ${item.event.sectors.join(", ")}` : ""}</span><time><Clock3 size={12} /> Опубликовано источником {formatPublicationTime(item.publishedAt)}</time>{item.receivedAt && <time>EventEdge получил {formatPublicationTime(item.receivedAt)}{item.deliveryLagMinutes === null ? "" : ` · лаг ${item.deliveryLagMinutes} мин`}</time>}</div>
        <h1>{item.title}</h1>
        <div className="reader-body"><p>{item.content}</p>{relatedSignals.length ? <p>EventEdge рассчитал {signalCountText} влияния события. Это оценка силы события, а не обещанная доходность акции.</p> : <p>Событие сохранено как <strong>{scopeMeta.label.toLocaleLowerCase("ru-RU")}</strong>-контекст. Сигнал из него пока не рассчитан.</p>}</div>
        {relatedSignals.length > 0 && <section className="reader-signal-list"><span>Связанные сигналы</span>{relatedSignals.map((signal) => <article key={signal.id}><div><strong>{signal.target?.label || signal.company || signal.ticker}</strong><small>{signal.target?.type === "sector" ? "Отрасль" : signal.target?.type === "market" ? "Рынок" : "Компания"}</small></div><Direction direction={signal.direction} /><b>{formatScore(signal.score)} п.</b><p>{signal.summary}</p></article>)}</section>}
        {item.sources?.length > 1 && <section className="reader-sources"><span>Подтверждающие публикации</span>{item.sources.map((source) => <a href={source.url} target="_blank" rel="noreferrer" key={`${source.name}-${source.url}`}>{source.name}<ArrowUpRight size={11} /></a>)}</section>}
        <footer><FileText size={13} /> Показан текст, полученный от источника. <a href={item.url} target="_blank" rel="noreferrer">Открыть оригинал <ArrowUpRight size={11} /></a></footer>
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
        const sourceRequest = fetch(apiUrl("/v1/sources"), { signal: controller.signal }).catch(() => null);
        const [signalResponse, newsResponse] = await Promise.all([
          fetch(apiUrl("/v1/signals?status=active&limit=100"), { signal: controller.signal }),
          fetch(apiUrl("/v1/news?limit=100"), { signal: controller.signal }),
        ]);
        const sourceResponse = await Promise.race([
          sourceRequest,
          new Promise((resolve) => window.setTimeout(() => resolve(null), 1200)),
        ]);
        if (!signalResponse.ok || !newsResponse.ok) throw new Error("API вернул ошибку. Попробуй обновить страницу.");
        const [signalPayload, newsPayload] = await Promise.all([signalResponse.json(), newsResponse.json()]);
        let sourcePayload = readCachedSourceRegistry();
        if (sourceResponse?.ok) {
          sourcePayload = await sourceResponse.json();
          cacheSourceRegistry(sourcePayload);
        }
        const allSignals = signalPayload.data.map(signalFromApi);
        const instrumentSignals = allSignals.filter((item) => !item.target || item.target.type === "instrument");
        const signalsById = new Map(allSignals.map((item) => [item.id, item]));
        const sourceNames = new Map((sourcePayload?.data || methodologySources.map((source) => ({ source_id: source.id, name: source.name }))).map((source) => [source.source_id, source.name]));
        const nextNews = groupNewsEvents(newsPayload.data.map((item) => newsFromApi(item, signalsById, sourceNames)));
        const evidenceByTicker = new Map();
        nextNews.forEach((item) => {
          if (item.signal) item.signal.evidence.push(item);
          (item.tickers || []).forEach((ticker) => {
            if (!evidenceByTicker.has(ticker)) evidenceByTicker.set(ticker, []);
            evidenceByTicker.get(ticker).push(item);
          });
        });
        const seenTickers = new Set();
        setSignals(instrumentSignals.filter((item) => {
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
          const assessedTickers = new Set(nextSignals.map((item) => item.ticker));
          const newsOnlySignals = instrumentSignals.filter((item) => !assessedTickers.has(item.ticker));
          setSignals([...nextSignals, ...newsOnlySignals].map((item) => ({ ...item, evidence: evidenceByTicker.get(item.ticker)?.slice(0, 8) || [] })));
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
    const refreshMs = 60000;
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
