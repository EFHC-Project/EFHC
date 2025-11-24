/**
======================================================================
== EFHC Bot WebApp — магазин EFHC/NFT                                 ==
======================================================================
Назначение: показывает каталог пакетов EFHC и VIP NFT, без выполнения оплаты на клиенте.
Инварианты: цена 0 = карточка выключена; покупки идут через TON watcher и банк с Idempotency-Key, без автодоставки NFT.
ИИ-защита: рендер безопасен при пустом каталоге, не дублирует заказы.
Запреты: нет прямых денежных POST с клиента, нет EFHC→kWh, нет P2P.
======================================================================
*/
import { useEffect, useState } from "react";

import ShopGrid from "../components/ShopGrid";
import { ApiClient, ShopItemView } from "../lib/api";

const api = new ApiClient();

function ShopPage() {
  const [items, setItems] = useState<ShopItemView[]>([]);

  useEffect(() => {
    api.getShop().then(setItems);
  }, []);

  return (
    <main>
      <ShopGrid items={items} />
    </main>
  );
}

export default ShopPage;

// ======================================================================
// Пояснения «для чайника»:
//   • Покупка фактически совершается на сервере (TON watcher → банк EFHC).
//   • Карточки с price=0 отображаются как выключенные.
//   • Клиент не генерирует Idempotency-Key и не списывает средства.
// ======================================================================
