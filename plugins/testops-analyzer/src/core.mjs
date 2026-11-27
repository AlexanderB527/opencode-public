// Ядро расчёта плагина: чистые функции без сети и без диска.
//
// Источник истины — скрипты скилла testops-analysis:
//   scan_testops.py         → scan_launch / build_expected_set / _aggregate_status
//   dispatch_team_reruns.py → read_nonpass / build_tags / PASS_STATUSES
//
// Любое правило здесь обязано совпадать с ними. Расхождение — дефект плагина,
// а не «улучшение»: панель и скилл должны давать один и тот же @-список.
// Проверка: test/core.test.mjs (node --test).

// ── статусы ──────────────────────────────────────────────────────────────────

// scan_testops.py: NON_PASS_STATUSES = {"failed","broken","skipped"} — для СТАТИСТИКИ.
export const NON_PASS_STATUSES = Object.freeze(["failed", "broken", "skipped"]);
const NON_PASS = new Set(NON_PASS_STATUSES);

// dispatch_team_reruns.py: PASS_STATUSES = ("passed","skipped","pass","skip").
// 🔴 Для РАЗДАЧИ skipped считается PASS: пропущенный тест в @-список не попадает.
// Это разные множества, и путать их нельзя — иначе панель пришлёт в Jenkins
// номера, которые скилл бы не отправил.
export const RERUN_PASS = new Set(["passed", "skipped", "pass", "skip"]);

export const KNOWN_STATUSES = new Set(["passed", "failed", "broken", "skipped"]);

// _aggregate_status: "passed", если ВСЕ строки passed; иначе первый не-pass.
export function aggregateStatus(statuses) {
  const list = Array.isArray(statuses) ? statuses : [];
  if (!list.length) return "passed";
  if (list.every((s) => s === "passed")) return "passed";
  for (const s of list) if (NON_PASS.has(s)) return s;
  return list[0];
}

// Номер идёт в @-список, если агрегат не входит в PASS_STATUSES раздачи.
export function isRerunCandidate(aggregatedStatus) {
  return !RERUN_PASS.has(String(aggregatedStatus ?? "").toLowerCase());
}

// read_nonpass для new_tests: пропускаем, только если ВСЕ статусы — PASS.
export function newTestIsRerunCandidate(statuses) {
  const list = Array.isArray(statuses) ? statuses : [];
  if (!list.length) return true;
  return !list.every((s) => RERUN_PASS.has(String(s).toLowerCase()));
}

// ── номер теста и естественная сортировка ────────────────────────────────────

// Формат номера: «16.01.05.01 ПРОДКАТ …» — тот же regex, что в scan_testops.py.
export const TEST_NUMBER_RE = /(?<!\d)(\d{2}(?:\.\d{2,3})+)(?!\d)/;

export function testNumber(name) {
  const m = TEST_NUMBER_RE.exec(String(name ?? ""));
  return m ? m[1] : "";
}

// natural_key в скилле: числовые куски сравниваются как числа, прочие — как строки.
export function naturalKey(number) {
  return String(number ?? "")
    .split(".")
    .map((chunk) => (/^\d+$/.test(chunk) ? [0, Number(chunk)] : [1, chunk]));
}

export function compareNumbers(a, b) {
  const ka = naturalKey(a);
  const kb = naturalKey(b);
  const len = Math.max(ka.length, kb.length);
  for (let i = 0; i < len; i++) {
    const x = ka[i] ?? [-1, 0];
    const y = kb[i] ?? [-1, 0];
    if (x[0] !== y[0]) return x[0] - y[0];
    if (x[1] < y[1]) return -1;
    if (x[1] > y[1]) return 1;
  }
  return 0;
}

// ── фильтрация результатов ───────────────────────────────────────────────────

// Повторяет тело scan_launch (цикл по content) + build_expected_set (агрегат).
//
// Вход:
//   rows            — content всех страниц /api/v2/launch/{id}/test-result/flat
//   teamCanon       — канон выбранной команды (null — фильтра по команде нет)
//   etalon          — { exists, numbers:Set }
//   taggedIds       — Set(testCaseId) с тегом тестировщика
//   teamOfCase      — Map(testCaseId -> канон команды) уже нормализованный
//   launchTeam      — канон, выведенный из названия одно-командного запуска
//   teamByEtalonNumber — Map(номер -> канон) ТОЛЬКО для номеров, встречающихся
//                        ровно в одном эталоне (иначе неоднозначно — не гадаем)
//
// Выход — всё, что нужно панели, и ничего больше.
export function filterResults({
  rows = [],
  teamCanon = null,
  etalon = { exists: false, numbers: new Set() },
  taggedIds = new Set(),
  teamOfCase = new Map(),
  launchTeam = null,
  teamByEtalonNumber = new Map(),
} = {}) {
  const hasTeam = Boolean(teamCanon);
  const hasTag = taggedIds.size > 0;
  const etalonNumbers = etalon?.numbers ?? new Set();
  const etalonExists = Boolean(etalon?.exists);

  const rowCounts = { passed: 0, failed: 0, broken: 0, skipped: 0 };
  const launchTeamNumbers = new Set();
  const newNumbers = new Map();
  const perTeam = new Map();
  const byNumber = new Map();

  let included = 0;
  let includedByTag = 0;
  let matchedByTeam = 0;
  let teamRowsWithoutNumber = 0;
  let unknownStatusRows = 0;
  // Откуда взялась команда строки: поле Team / номер в эталоне / название запуска.
  const teamSource = { field: 0, etalon: 0, launch: 0, none: 0 };

  for (const row of rows) {
    const status = String(row?.status ?? "").toLowerCase();
    if (!KNOWN_STATUSES.has(status)) unknownStatusRows += 1;

    const number = testNumber(row?.name);
    const testCaseId = row?.testCaseId ?? null;

    // 1) поле Team тест-кейса (как в скилле), 2) номер в единственном эталоне,
    // 3) название одно-командного запуска. Шаги 2–3 — только когда поле пустое,
    // и каждый факт такого вывода виден в teamSource и в предупреждениях.
    let teamResolved = "";
    if (testCaseId !== null && teamOfCase.has(testCaseId)) {
      teamResolved = teamOfCase.get(testCaseId) || "";
    }
    let via = teamResolved ? "field" : "";
    if (!teamResolved && number && teamByEtalonNumber.has(number)) {
      teamResolved = teamByEtalonNumber.get(number);
      via = "etalon";
    }
    if (!teamResolved && launchTeam) {
      teamResolved = launchTeam;
      via = "launch";
    }

    const teamOk = hasTeam && teamResolved === teamCanon;
    if (teamOk) {
      teamSource[via || "none"] += 1;
      if (number) launchTeamNumbers.add(number);
      else teamRowsWithoutNumber += 1;
    }

    // G1 скилла: тест своей команды, которого нет в эталоне — кандидат в эталон.
    if (number && teamOk && etalonExists && !etalonNumbers.has(number)) {
      const known = newNumbers.get(number);
      if (known) known.statuses.push(status);
      else newNumbers.set(number, { number, name: String(row?.name ?? ""), team: teamResolved, statuses: [status] });
    }

    const etalonOk = etalonExists ? etalonNumbers.has(number) : true;
    const byTeam = teamOk && etalonOk;
    const tagOk = hasTag && testCaseId !== null && taggedIds.has(testCaseId);

    // Без команды и без тега — весь запуск (режим «что вообще в launch»).
    const include = !hasTeam && !hasTag ? true : (hasTag ? (byTeam || tagOk) : byTeam);
    if (!include) continue;

    included += 1;
    const origin = !hasTeam && !hasTag ? "all" : byTeam && tagOk ? "both" : tagOk ? "tag" : "team";
    if (origin === "tag") includedByTag += 1;
    if (byTeam) matchedByTeam += 1;
    if (rowCounts[status] !== undefined) rowCounts[status] += 1;

    // Ключ — номер теста. Строки без номера попадают в один общий бакет "" —
    // ровно как order/statuses в build_expected_set (иначе total и pass_rate
    // расходятся со скиллом на каждом тесте без номера в имени).
    const key = number;
    let entry = byNumber.get(key);
    if (!entry) {
      entry = {
        number,
        name: String(row?.name ?? ""),
        team: teamResolved || null,
        origin,
        testCaseId,
        resultId: row?.id ?? null,
        statuses: [],
      };
      byNumber.set(key, entry);
    } else {
      // Объединение происхождения как в скилле:
      // prev != origin → "both" только для пары {team, tag}, иначе остаётся prev.
      if (entry.origin !== origin) {
        const pair = new Set([entry.origin, origin]);
        entry.origin = pair.size === 2 && pair.has("team") && pair.has("tag") ? "both" : entry.origin;
      }
      if (!entry.team && teamResolved) entry.team = teamResolved;
      if (!entry.resultId && row?.id) entry.resultId = row?.id;
    }
    entry.statuses.push(status);
  }

  // Агрегат по номеру (build_expected_set).
  const numberCounts = { passed: 0, failed: 0, broken: 0, skipped: 0 };
  const nonPassed = [];
  const rerunNumbers = [];
  const skippedNumbers = [];
  let otherAggregate = 0;

  for (const entry of byNumber.values()) {
    const agg = aggregateStatus(entry.statuses);
    if (numberCounts[agg] !== undefined) numberCounts[agg] += 1;
    else otherAggregate += 1;

    if (!NON_PASS.has(agg)) continue;
    const rerun = isRerunCandidate(agg);
    if (entry.number && rerun) rerunNumbers.push(entry.number);
    else if (entry.number) skippedNumbers.push(entry.number);
    if (entry.team) perTeam.set(entry.team, (perTeam.get(entry.team) ?? 0) + 1);
    nonPassed.push({
      number: entry.number,
      name: entry.name,
      status: agg,
      statuses: entry.statuses,
      count: entry.statuses.length,
      team: entry.team,
      origin: entry.origin,
      testCaseId: entry.testCaseId,
      resultId: entry.resultId,
      rerun,
    });
  }

  const newTests = [...newNumbers.values()].map((e) => ({
    number: e.number,
    name: e.name,
    team: e.team,
    statuses: e.statuses,
  }));
  const newFailed = newTests.filter((e) => newTestIsRerunCandidate(e.statuses));

  const total = byNumber.size;
  const passed = numberCounts.passed;

  return {
    byNumber,
    numberCounts,
    rowCounts,
    nonPassed,
    newTests,
    newFailed,
    rerunNumbers: rerunNumbers.sort(compareNumbers),
    skippedNumbers: skippedNumbers.sort(compareNumbers),
    launchTeamNumbers,
    teamRowsWithoutNumber,
    unknownStatusRows,
    otherAggregate,
    included,
    includedByTag,
    matchedByTeam,
    perTeam,
    teamSource,
    total,
    passed,
    passRate: total > 0 ? Math.round((passed / total) * 1000) / 10 : null,
  };
}

// ── охват эталона ────────────────────────────────────────────────────────────

// scan_testops.py считает охват в направлении «сколько эталона прогнано»:
// covered = эталон ∩ номера своей команды в запуске; missing — остаток.
export function etalonCoverage(etalonNumbers, launchTeamNumbers) {
  const norm = (s) => String(s).trim().toLowerCase();
  const etalon = new Set([...etalonNumbers].map(norm));
  const ran = new Set([...launchTeamNumbers].map(norm));
  const covered = [...etalon].filter((n) => ran.has(n));
  const missing = [...etalon].filter((n) => !ran.has(n));
  const pct = etalon.size ? Math.round((covered.length / etalon.size) * 1000) / 10 : null;
  const overlapLaunch = [...ran].filter((n) => etalon.has(n)).length;
  const pctLaunch = ran.size ? Math.round((overlapLaunch / ran.size) * 1000) / 10 : null;
  return {
    total: etalon.size,
    covered: covered.length,
    missing: missing.length,
    missingNumbers: missing.sort(compareNumbers),
    pct,
    overlapLaunch,
    pctLaunch,
  };
}

// 🔴 Гейт скилла (scan_testops.py, «Контроль эталона уровнем 2»): если номера
// команды в запуске НЕ пересекаются с эталоном вообще (overlap == 0), значит
// выбран неверный тип (QG/RM/HF) или неверная команда. Скилл в этом случае
// печатает «НЕВЕРНЫЙ ЭТАЛОН», НЕ пишет артефакты и запрещает публикацию.
//
// Почему это гейт, а не предупреждение: без него каждый тест команды выглядит
// «новым вне эталона» и уходит в @-список. Проверено на 1344527: TREX с эталоном
// RM (25 номеров, запуск QG) — плагин собирал 188 номеров на перезапуск, тогда
// как скилл отказывался работать вовсе.
//
// Условие совпадает со скиллом буквально: эталон есть, номера команды в запуске
// есть, пересечение нулевое. Неполный охват (overlap > 0) — НЕ гейт.
export function etalonMismatch({ etalonExists, launchTeamCount, overlapLaunch }) {
  return Boolean(etalonExists) && Number(launchTeamCount) > 0 && Number(overlapLaunch) === 0;
}

// ── тип прогона ──────────────────────────────────────────────────────────────

// Джоба DEBUG/<TEAM> — это джоба ПЕРЕЗАПУСКА упавших, её запуски по определению
// рераны. Проверено на launch 1363380: имя «DEBUG - qa (DEBUG/TX-T9 - #9)»,
// а сам состав 152/283 НЕ отличается от основного — без этого правила тип
// определялся бы как «основной».
//
// 🔴 По СОСТАВУ запуска тип не определяем. Маленький основной прогон
// неотличим от рерана числом строк (15 из 283 — это 5% эталона, но это может
// быть и честный смоук), а полный реран — от основного. Любая пороговая
// эвристика даёт ложные срабатывания в одну из сторон; надёжный признак
// рерана — только имя джобы/маркер в названии. Нет маркера → «основной»
// с низкой уверенностью, и пользователь решает сам (runKind: main/rerun).
const RERUN_JOB_RE = /\bDEBUG\s*\/\s*[A-Za-zА-Яа-я0-9_-]+/i;
const RERUN_NAME_RE = /(rerun|re-run|реран|retry|повтор)/i;

// Возвращает { value, source, confidence, detected, reasons }.
// confidence: "high" — факт (имя джобы / явный маркер / выбор пользователя),
//             "low"  — признаков нет, принято «основной».
export function detectRunKind({ name, rowsCount = 0, etalonCount = 0, requested = "auto" } = {}) {
  const req = String(requested ?? "auto").toLowerCase();
  if (req === "main" || req === "rerun") {
    return { value: req, source: "user", confidence: "high", detected: req, reasons: [] };
  }

  const text = String(name ?? "");
  const reasons = [];
  let detected = "main";
  let confidence = "low";

  if (RERUN_JOB_RE.test(text)) {
    detected = "rerun";
    confidence = "high";
    reasons.push("запуск создан джобой DEBUG/<TEAM> — это джоба перезапуска упавших");
  } else if (RERUN_NAME_RE.test(text)) {
    detected = "rerun";
    confidence = "high";
    reasons.push("в названии запуска есть признак перезапуска");
  } else if (!etalonCount) {
    reasons.push("эталона нет — тип прогона определить нечем, принято «основной»");
  } else {
    reasons.push(
      `состав ${rowsCount} строк при эталоне ${etalonCount} номеров — `
        + "по составу тип не определяем (см. комментарий), принято «основной»; "
        + "если это перезапуск упавших — укажи runKind=rerun",
    );
  }

  return { value: detected, source: "detected", confidence, detected, reasons };
}
