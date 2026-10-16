// Composition root: game modules do not import one another or own login/browser lifetime.
import { createClientHost } from "./client_host.mjs";
import { createClientNetwork, sourceStep } from "./client_network.mjs";
import { createGenshinSource, sourceNames as genshinNames } from "./games/genshin/client_source.mjs";
import { createZzzSource, sourceNames as zzzNames } from "./games/zzz/client_source.mjs";

export function createRosterHost(browser) {
  const network = createClientNetwork(browser);
  const sources = { genshin: createGenshinSource(network), zzz: createZzzSource(network) };
  return createClientHost(browser, {
    sourceNames: { genshin: genshinNames, zzz: zzzNames },
    serviceSources: { zzz: { cultivate: "cultivate:badge-login" } },
    checkSources: query => sourceStep(query, active => sources[active.game].available(active)),
    readSource: query => sourceStep(query, active => sources[active.game].read(active)),
    prepareService: query => sourceStep(query, active => sources[active.game].prepare(active)),
  });
}
