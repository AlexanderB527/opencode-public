// Чистые функции сопоставления для qa-artifact-guard.
//
// Вынесены из index.ts, чтобы проверяться юнит-тестами без запуска OpenCode
// (см. matchers.test.ts, запуск: node --test plugins/qa-artifact-guard/lib/).

/** Действия, которые пишут в файлы. */
export const WRITE_ACTIONS = new Set(["edit", "write", "patch", "multiedit", "apply_patch"]);

/** Действия, исполняющие shell. */
export const SHELL_ACTIONS = new Set(["shell", "bash", "run_command"]);

/**
 * Скрипты скилла `testops-analysis`, которым РАЗРЕШЕНО писать в `~/.qa-artifacts`.
 *
 * Это и есть «работа со скиллом»: плагин знает точки входа скилла и отличает
 * санкционированную запись (скрипт сам кладёт артефакты) от ручной подмены.
 *
 * 🔴 `update_etalon.py` здесь НЕТ намеренно: он пишет в эталоны, а эталоны
 * защищены абсолютно (решение пользователя, не агента).
 */
export const SKILL_WRITE_SCRIPTS = [
  "scan_testops.py",
  // analyze_launch.py упоминается в документации скилла как точка входа разбора;
  // если появится таким именем — запись разрешена.
  "analyze_launch.py",
  "scan_all_teams.py",
  "watch_launch.py",
  "wait_team_reruns.py",
  "dispatch_team_reruns.py",
  "diff_rerun.py",
  "classify_rerun.py",
  "merge_launches.py",
  "diagnose_skips.py",
  "render_markdown.py",
  "verify_sets.py",
  "doctor.py",
  "fetch_etalons.py",
  "git_data.py",
  "migrate_data_branch.py",
];

/** Признаки записи в shell-команде. */
export const SHELL_WRITE_MARKERS = [
  // PowerShell
  "set-content",
  "out-file",
  "add-content",
  "tee-object",
  "new-item",
  "copy-item",
  "move-item",
  "remove-item",
  "rename-item",
  // POSIX
  "tee ",
  "cp ",
  "mv ",
  "rm ",
  "touch ",
  "truncate ",
  "dd ",
  "chmod ",
  "update_etalon",
];

/** Признаки inline-кода, который может писать в файлы. */
const INLINE_CODE_MARKERS = ["py -c", "python -c", "node -e", "pwsh -c", "powershell -c"];

/**
 * Приводит путь или команду к единому виду для сравнения.
 *
 * Пути приходят ОТНОСИТЕЛЬНЫМИ каталога (`.qa-artifacts/x.json`, `.\\a\\b`),
 * поэтому нельзя искать только `/`-обрамлённое вхождение: нужен нормализованный
 * вид с гарантированными краями.
 */
export function norm(value) {
  return String(value ?? "").replace(/\\/g, "/").toLowerCase();
}

/**
 * Проверяет, указывает ли ресурс на каталог `needle`.
 *
 * Совпадение засчитывается, когда путь:
 *   - начинается с каталога (`.qa-artifacts/x.json`);
 *   - содержит его как сегмент (`<local-path>`);
 *   - заканчивается им (`.qa-artifacts`).
 */
export function pathHasSegment(resource, needle) {
  const path = norm(resource);
  if (!path) return false;
  if (path.startsWith(`${needle}/`) || path === needle) return true;
  return path.includes(`/${needle}/`) || path.endsWith(`/${needle}`);
}

/** Путь внутри каталога артефактов разбора. */
export function isQaArtifacts(resource) {
  return pathHasSegment(resource, ".qa-artifacts");
}

/** Путь внутри каталога эталонов скилла. */
export function isEtalon(resource) {
  const path = norm(resource);
  return path.includes("testops-analysis/etalons") || path.startsWith("etalons/");
}

/** Путь к правилам классификации. */
export function isClassifyRules(resource) {
  return norm(resource).includes("classify_rules.json");
}

/** Команда вызывает санкционированный скрипт скилла. */
export function isSkillScriptInvocation(command) {
  const cmd = norm(command);
  if (!cmd) return false;
  return SKILL_WRITE_SCRIPTS.some((script) => cmd.includes(script));
}

/** Команда содержит признаки записи в файлы. */
export function hasShellWriteMarkers(command) {
  const cmd = norm(command);
  if (!cmd) return false;
  if (SHELL_WRITE_MARKERS.some((marker) => cmd.includes(marker))) return true;
  if (INLINE_CODE_MARKERS.some((marker) => cmd.includes(marker))) return true;
  // Редирект вывода: ">" / ">>", но не "2>&1" (там перед ">" стоит цифра).
  return /(?:^|[^0-9])>>?(?!&)/.test(cmd);
}

/**
 * Команда указывает на каталог записью-параметром вывода.
 *
 * Нужно для команд вида `py script.py --out etalons/T9/etalon_qg.txt`,
 * где запись идёт без shell-маркеров и без редиректа.
 */
export function hasOutputArgument(command) {
  const cmd = norm(command);
  // --out / --output / -out / --out=... (но не --out-dir-прочее)
  return /(?:^|\s)--?(?:out|output)(?:[=\s]|$)/.test(cmd);
}

/**
 * Команда упоминает каталог как СТРОКА, а не как путь целиком.
 *
 * Для shell `resources` приходит вся команда (`Set-Content .qa-artifacts/...`),
 * поэтому сегментная проверка пути неприменима — ищем подстроку.
 */
export function commandMentions(command, needle) {
  return norm(command).includes(norm(needle));
}

/**
 * Причина запрета записи в ресурс.
 * @returns {string | null} причина либо null, если запись разрешена
 */
export function denyReasonForWrite(resource) {
  if (isQaArtifacts(resource)) {
    return (
      "запись в ~/.qa-artifacts/** запрещена: артефакты создаёт только скрипт скилла. " +
      "Запусти analyze_launch.py / scan_testops.py вместо ручной записи файла."
    );
  }
  if (isEtalon(resource)) {
    return (
      "правка эталона команды запрещена: эталон — источник истины по составу команды. " +
      "Изменить эталон может только пользователь через update_etalon.py."
    );
  }
  if (isClassifyRules(resource)) {
    return (
      "правка config/classify_rules.json по ходу разбора запрещена: " +
      "подгонка правил под результат = обход классификации."
    );
  }
  return null;
}

/**
 * Причина запрета shell-команды.
 *
 * Логика:
 *   1. etalons — защита абсолютная: любая пишущая команда отклоняется;
 *   2. ~/.qa-artifacts — разрешено ТОЛЬКО санкционированным скриптам скилла;
 *      чтение (листинг, cat) не блокируется;
 *   3. прочие команды — не наше дело.
 *
 * @returns {string | null} причина либо null, если команда разрешена
 */
export function denyReasonForShell(command) {
  const cmd = norm(command);
  if (!cmd) return null;

  // 1. Эталоны — без исключений. Пишущей считаем команду, которая либо несёт
  //    shell-маркер записи, либо передаёт путь эталона как аргумент вывода.
  if (cmd.includes("etalon")) {
    const writesEtalon = hasShellWriteMarkers(cmd) || hasOutputArgument(cmd) || cmd.includes("update_etalon");
    if (writesEtalon) {
      return (
        "shell-запись в etalons/** требует явного решения пользователя. " +
        "Пришли команду пользователю — он подтвердит и запустит сам."
      );
    }
  }

  // 2. Артефакты разбора — только через скрипты скилла.
  if (commandMentions(cmd, ".qa-artifacts")) {
    if (isSkillScriptInvocation(cmd)) return null;
    if (hasShellWriteMarkers(cmd)) {
      return (
        "shell-запись в ~/.qa-artifacts/** запрещена: артефакты создаёт только скрипт скилла " +
        "(scan_testops.py / analyze_launch.py / diff_rerun.py). Пересканируй вместо правки файла."
      );
    }
  }

  return null;
}

/**
 * Решает, блокировать ли событие прав.
 *
 * @param {{ action?: string, resources?: readonly string[] }} event событие хука
 * @param {readonly string[]} disabled выключенные проверки
 * @returns {string | null} причина блокировки либо null
 */
export function decideDeny(event, disabled = []) {
  const off = disabled.map(norm);
  if (off.includes("all")) return null;

  const action = norm(event?.action);
  const resources = event?.resources ?? [];

  if (WRITE_ACTIONS.has(action)) {
    for (const resource of resources) {
      if (off.includes("qa-artifacts") && isQaArtifacts(resource)) continue;
      if (off.includes("etalons") && isEtalon(resource)) continue;
      if (off.includes("classify-rules") && isClassifyRules(resource)) continue;
      const reason = denyReasonForWrite(resource);
      if (reason) return reason;
    }
    return null;
  }

  if (SHELL_ACTIONS.has(action)) {
    for (const resource of resources) {
      // Точечное выключение: shell-запись в эталоны и shell-запись в артефакты.
      if (off.includes("shell-etalons") && norm(resource).includes("etalon")) continue;
      if (off.includes("shell-artifacts") && commandMentions(resource, ".qa-artifacts")) continue;
      const reason = denyReasonForShell(resource);
      if (reason) return reason;
    }
  }

  return null;
}
