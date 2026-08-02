import "@testing-library/jest-dom/vitest";

import en from "../../custom_components/phoenix_mcp/catalogs/en.json";
import { primeTranslations } from "../i18n";

// The panel fetches its strings from Phoenix's own admin API at boot. Tests
// issue no requests, so prime the real catalog synchronously: assertions on
// English text keep working untouched, and a key that is missing from en.json
// fails loudly in whichever test renders it.
primeTranslations(en.panel);
