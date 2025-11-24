/**
======================================================================
== EFHC Bot WebApp — главная (энергия и панели)                      ==
======================================================================
Назначение: показывает текущую энергию, статус VIP и активные панели; не проводит денежные операции.
Инварианты: данные берутся из API; клиент не пересчитывает генерацию и не продаёт панели сам.
ИИ-защита: мягкая загрузка — при ошибке сети отображает пустые значения без падения.
Запреты: нет EFHC→kWh, нет покупок без Idempotency-Key, нет P2P.
======================================================================
*/
import { useEffect, useState } from "react";

import EnergyGauge from "../components/EnergyGauge";
import ExchangePanel from "../components/ExchangePanel";
import PanelsList from "../components/PanelsList";
import { ApiClient, PanelView } from "../lib/api";

const api = new ApiClient();

function HomePage() {
  const [availableKwh, setAvailableKwh] = useState(0);
  const [totalGeneratedKwh, setTotalGeneratedKwh] = useState(0);
  const [isVip, setIsVip] = useState(false);
  const [panels, setPanels] = useState<PanelView[]>([]);

  useEffect(() => {
    api.getEnergy().then((energy) => {
      setAvailableKwh(energy.availableKwh);
      setTotalGeneratedKwh(energy.totalGeneratedKwh);
      setIsVip(energy.isVip);
    });

    api.getPanels().then(setPanels);
  }, []);

  return (
    <main>
      <EnergyGauge
        availableKwh={availableKwh}
        totalGeneratedKwh={totalGeneratedKwh}
        isVip={isVip}
      />
      <ExchangePanel availableKwh={availableKwh} />
      <PanelsList
        panels={panels.map((panel) => ({
          id: panel.id,
          expiresAt: panel.expiresAt,
          dailyKwh: panel.dailyKwh,
        }))}
      />
    </main>
  );
}

export default HomePage;

// ======================================================================
// Пояснения «для чайника»:
//   • Страница читает энергию и панели с сервера, без локальных расчётов.
//   • Никаких денежных действий здесь нет — только отображение.
//   • VIP определяется сервером (NFT), клиент только показывает флаг.
//   • При ошибке сети увидите нули/пустые списки вместо падения.
// ======================================================================
