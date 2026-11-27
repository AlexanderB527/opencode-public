// Юнит-тесты сопоставления путей и решений qa-artifact-guard.
// Запуск: node --test "plugins/qa-artifact-guard/lib/*.test.ts"
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  decideDeny,
  denyReasonForShell,
  hasShellWriteMarkers,
  isClassifyRules,
  isEtalon,
  isQaArtifacts,
  isSkillScriptInvocation,
  pathHasSegment,
} from "./matchers.ts";

// ---------------------------------------------------------------- пути

test("артефакты: относительный путь ловится (регресс — пропуск в первой версии)", () => {
  assert.equal(isQaArtifacts(".qa-artifacts/case_meta.json"), true);
  assert.equal(isQaArtifacts(".qa-artifacts"), true);
  assert.equal(isQaArtifacts(".qa-artifacts/"), true);
});

test("артефакты: абсолютный путь и другой пользователь ловятся", () => {
  assert.equal(isQaArtifacts("C:/Users/fixture-user-1/.qa-artifacts/case_meta.json"), true);
  assert.equal(isQaArtifacts("C:\\Users\\Alfa\\.qa-artifacts\\x.json"), true);
  assert.equal(isQaArtifacts("/home/other/.qa-artifacts/x.json"), true);
});

test("артефакты: похожие, но чужие пути не блокируются", () => {
  assert.equal(isQaArtifacts("docs/.qa-artifacts-guide.md"), false);
  assert.equal(isQaArtifacts("skills/testops-analysis/scripts/scan_testops.py"), false);
  assert.equal(isQaArtifacts(""), false);
  assert.equal(isQaArtifacts(undefined), false);
});

test("эталоны: путь внутри скилла и относительный etalons/", () => {
  assert.equal(isEtalon("skills/testing-skills/testops-analysis/etalons/T9/etalon_qg.txt"), true);
  assert.equal(isEtalon("skills\\testing-skills\\testops-analysis\\etalons\\T9\\etalon_qg.txt"), true);
  assert.equal(isEtalon("etalons/T9/etalon_qg.txt"), true);
  assert.equal(isEtalon("skills/testops-analysis/README.md"), false);
});

test("правила классификации", () => {
  assert.equal(isClassifyRules("skills/testing-skills/testops-analysis/config/classify_rules.json"), true);
  assert.equal(isClassifyRules("config/team_jobs.json"), false);
});

test("pathHasSegment: не срабатывает на частичные совпадения", () => {
  assert.equal(pathHasSegment("a/myb-c/x", "b-c"), false);
  assert.equal(pathHasSegment("a/b-c/x", "b-c"), true);
});

// ------------------------------------------------- запись через edit

test("decideDeny: запись в артефакты блокируется действием edit", () => {
  const reason = decideDeny({ action: "edit", resources: [".qa-artifacts/case_meta.json"] });
  assert.ok(reason && reason.includes("~/.qa-artifacts"), `ожидалась причина, получено: ${reason}`);
});

test("decideDeny: чтение не блокируется", () => {
  assert.equal(decideDeny({ action: "read", resources: [".qa-artifacts/case_meta.json"] }), null);
});

test("decideDeny: обычная правка репозитория не блокируется", () => {
  assert.equal(decideDeny({ action: "edit", resources: ["skills/testops-analysis/SKILL.md"] }), null);
});

test("decideDeny: выключение конкретной проверки работает", () => {
  const event = { action: "edit", resources: [".qa-artifacts/x.json"] };
  assert.ok(decideDeny(event, []));
  assert.equal(decideDeny(event, ["qa-artifacts"]), null);
  assert.equal(decideDeny(event, ["all"]), null);
});

test("decideDeny: выключение артефактов не снимает защиту эталонов", () => {
  const event = { action: "edit", resources: ["skills/testops-analysis/etalons/T9/etalon_qg.txt"] };
  assert.ok(decideDeny(event, ["qa-artifacts"]));
});

// --------------------------------------- работа со скриптами скилла (shell)

test("скилл: санкционированные скрипты распознаются", () => {
  assert.equal(isSkillScriptInvocation("py -X utf8 scripts/scan_testops.py --launch-id 1"), true);
  assert.equal(isSkillScriptInvocation("py -X utf8 scripts/diff_rerun.py --rerun 2"), true);
  assert.equal(isSkillScriptInvocation("py -X utf8 scripts/dispatch_team_reruns.py"), true);
  assert.equal(isSkillScriptInvocation("py -X utf8 my_own_script.py"), false);
  assert.equal(isSkillScriptInvocation(""), false);
});

test("скилл: сканирование — разрешено, даже если путь артефактов в команде", () => {
  const cmd = "py -X utf8 scripts/scan_testops.py --launch-id 1327498 --root .qa-artifacts";
  assert.equal(denyReasonForShell(cmd), null);
});

test("скилл: разбор рерана — разрешён", () => {
  const cmd = "py -X utf8 scripts/diff_rerun.py --rerun 1334823 --launch 1327498 --root .qa-artifacts";
  assert.equal(denyReasonForShell(cmd), null);
});

test("ручная запись в артефакты через shell — блокируется", () => {
  assert.ok(denyReasonForShell("Set-Content .qa-artifacts/launches/1/T9/expected_set.json -Value x"));
  assert.ok(denyReasonForShell("echo tampered > .qa-artifacts/launches/1/T9/scan_manifest.json"));
  assert.ok(denyReasonForShell("cp fake.json .qa-artifacts/launches/1/T9/expected_set.json"));
  assert.ok(denyReasonForShell("rm .qa-artifacts/launches/1 -Recurse"));
});

test("inline-код, пишущий в артефакты, — блокируется", () => {
  assert.ok(denyReasonForShell("py -c \"open('.qa-artifacts/x.json','w').write('{}')\""));
  assert.ok(denyReasonForShell("node -e \"require('fs').writeFileSync('.qa-artifacts/x.json','{}')\""));
});

test("чтение артефактов через shell — не блокируется", () => {
  assert.equal(denyReasonForShell("Get-ChildItem .qa-artifacts -Recurse"), null);
  assert.equal(denyReasonForShell("Get-Content .qa-artifacts/launches/1/T9/expected_set.json"), null);
  assert.equal(denyReasonForShell("cat .qa-artifacts/launches/1/T9/dispatch.json"), null);
});

test("shell вне артефактов и эталонов — не наше дело", () => {
  assert.equal(denyReasonForShell("git status"), null);
  assert.equal(denyReasonForShell("node --test plugins/qa-artifact-guard/lib/*.test.ts"), null);
  assert.equal(denyReasonForShell("cmd /c \"opencode plugin list\""), null);
});

test("редирект: 2>&1 не считается записью, а > считается", () => {
  assert.equal(hasShellWriteMarkers("py script.py 2>&1"), false);
  assert.equal(hasShellWriteMarkers("echo x > out.txt"), true);
  assert.equal(hasShellWriteMarkers("echo x >> out.txt"), true);
});

// ------------------------------------------------ эталоны: без исключений

test("эталоны: запись блокируется, чтение — нет", () => {
  assert.ok(denyReasonForShell("Set-Content etalons/T9/etalon_qg.txt -Value x"));
  assert.ok(denyReasonForShell("cp a.txt etalons/T9/etalon_qg.txt"));
  assert.equal(denyReasonForShell("Get-Content etalons/T9/etalon_qg.txt"), null);
});

test("эталоны: update_etalon.py не в белом списке — запись требует пользователя", () => {
  assert.ok(denyReasonForShell("py -X utf8 scripts/update_etalon.py --add 12345"));
});

test("эталоны: белый список скриптов не открывает запись в эталоны", () => {
  // даже санкционированный скрипт, пишущий в etalons, блокируется
  assert.ok(denyReasonForShell("py scripts/scan_testops.py --out etalons/T9/etalon_qg.txt"));
});

test("shell: действие shell ловит запись в etalons", () => {
  const reason = decideDeny({ action: "shell", resources: ["update_etalon.py --add 12345"] });
  assert.ok(reason);
});

test("shell: сканирование через decideDeny проходит", () => {
  const reason = decideDeny({
    action: "shell",
    resources: ["py -X utf8 scripts/scan_testops.py --launch-id 1 --root .qa-artifacts"],
  });
  assert.equal(reason, null);
});

test("shell: ручная запись через decideDeny блокируется", () => {
  const reason = decideDeny({
    action: "shell",
    resources: ["Set-Content .qa-artifacts/launches/1/T9/scan_manifest.json -Value x"],
  });
  assert.ok(reason);
});

test("shell: точечное выключение shell-artifacts работает", () => {
  const event = { action: "shell", resources: ["echo x > .qa-artifacts/y.json"] };
  assert.ok(decideDeny(event, []));
  assert.equal(decideDeny(event, ["shell-artifacts"]), null);
});
