import { createApp } from "vue";
import App from "./App.vue";
import { router } from "./router";
import { applyTheme } from "./theme";
import "./style/tokens.css";
import "./style/base.css";

applyTheme();

createApp(App).use(router).mount("#app");
