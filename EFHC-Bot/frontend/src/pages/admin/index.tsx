/**
======================================================================
== EFHC Bot WebApp — админ главная                                    ==
======================================================================
Назначение: сводный экран метрик для админов; все действия выполняются сервером после require_admin_or_nft_or_key.
Инварианты: клиент не выполняет корректировки и не проводит деньги; только отображает данные.
ИИ-защита: безопасная загрузка метрик с fallback на пустой список.
Запреты: нет локальных админ-действий, нет денежных операций.
======================================================================
*/
import { useEffect, useState } from "react";

import AdminCharts from "../../components/AdminCharts";
import AdminTable from "../../components/AdminTable";
import { AdminMetric, ApiClient } from "../../lib/api";

const api = new ApiClient();

function AdminHomePage() {
  const [metrics, setMetrics] = useState<AdminMetric[]>([]);

  useEffect(() => {
    api.getAdminMetrics().then(setMetrics);
  }, []);

  return (
    <main>
      <AdminCharts metrics={metrics} />
      <AdminTable rows={metrics.map((metric) => ({ label: metric.label, value: metric.value }))} />
    </main>
  );
}

export default AdminHomePage;

// ======================================================================
// Пояснения «для чайника»:
//   • Админ-метрики приходят с сервера; UI их не изменяет.
//   • Доступ проверяет сервер (телеграм ID, NFT или api-key), не фронт.
//   • Денежные операции возможны только через админ-API, не здесь.
// ======================================================================
