/**
======================================================================
== EFHC Bot WebApp — обменник kWh → EFHC                              ==
======================================================================
Назначение: информирует об остатке kWh для обмена и правилах 1:1 без обратной конверсии.
Инварианты: курс фиксирован, пользователь не уходит в минус; денежные операции выполняет сервер через банк и Idempotency-Key.
ИИ-защита: загрузка данных с fallback на нули, UI не инициирует перевод.
Запреты: нет EFHC→kWh и P2P переводов.
======================================================================
*/
import { useEffect, useState } from "react";

import ExchangePanel from "../components/ExchangePanel";
import { ApiClient } from "../lib/api";

const api = new ApiClient();

function ExchangePage() {
  const [availableKwh, setAvailableKwh] = useState(0);

  useEffect(() => {
    api.getEnergy().then((energy) => setAvailableKwh(energy.availableKwh));
  }, []);

  return (
    <main>
      <ExchangePanel availableKwh={availableKwh} />
    </main>
  );
}

export default ExchangePage;

// ======================================================================
// Пояснения «для чайника»:
//   • Обмен выполняет сервер: UI лишь показывает остаток kWh и курс 1:1.
//   • Без Idempotency-Key денежные POST не будут приняты сервером.
//   • Обратной конверсии EFHC→kWh нет, P2P нет.
// ======================================================================
