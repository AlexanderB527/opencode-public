import { defineConfig } from "vite"
import { gui } from "@opencode/plugin/gui/vite"

// Десктоп грузит GUI-плагин только как prebuilt CommonJS-бандл,
// поэтому собираем ровно один файл gui-experimental.cjs (как у плагина assistant).
export default defineConfig({
  plugins: [gui({ entry: "src/gui.tsx", outFile: "gui-experimental.cjs" })],
})
