// Composition root: game modules do not import one another or own login/browser lifetime.
import { createClientHost } from "./client_host.mjs";
import { createClientNetwork, sourceStep } from "./client_network.mjs";
import { createGenshinSource, sourceNames as genshinNames } from "./games/genshin/client_source.mjs";
import { createZzzSource, sourceNames as zzzNames } from "./games/zzz/client_source.mjs";
import { createPageBrowser, pageSourceNames } from "./client_page.mjs";
import { createGenshinPageSource } from "./games/genshin/page_source.mjs";
import { createZzzPageSource } from "./games/zzz/page_source.mjs";

export function createRosterHost(browser) {
  const network = createClientNetwork(browser);
  const sources = { genshin: createGenshinSource(network), zzz: createZzzSource(network) };
  const ui = createPageBrowser(browser);
  const pages = { genshin: createGenshinPageSource(ui), zzz: createZzzPageSource(ui) };
  const host = createClientHost(browser, {
    pageReader: true,
    sourceNames: { genshin: [...genshinNames, ...pageSourceNames], zzz: [...zzzNames, ...pageSourceNames] },
    serviceSources: { zzz: { cultivate: "cultivate:badge-login" } },
    checkSources: query => sourceStep(query, active =>
      active.sources.every(name => name.startsWith("page:")) ? pages[active.game].available(active) : sources[active.game].available(active)),
    readSource: query => sourceStep(query, active =>
      (active.source.startsWith("page:") ? pages : sources)[active.game].read(active)),
    prepareService: query => sourceStep(query, active => sources[active.game].prepare(active)),
  });
  const startTask = host.startTask.bind(host);
  host.releasePageSettings = ui.release;
  host.startTask = options => {
    const task = startTask(options);
    const pump = task.pump.bind(task);
    task.pump = async options => {
      const state = await pump(options);
      if (state.done) await ui.release();
      return state;
    };
    return task;
  };
  return host;
}
