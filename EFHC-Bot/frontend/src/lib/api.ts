/**
======================================================================
== EFHC Bot WebApp — клиент API                                      ==
======================================================================
Назначение: безопасная обёртка над fetch для чтения данных EFHC без перерасчётов на клиенте.
Инварианты: клиент не меняет балансы и не инициирует денежные операции; все суммы отображаются как получили от бэкенда.
ИИ-защита: мягкая деградация — при ошибках возвращает пустые структуры, не создаёт повторных запросов без нужды.
Запреты: не выполняет POST с деньгами, не формирует Idempotency-Key — это ответственность бэкенда и ботов.
======================================================================
*/
export type EnergySnapshot = {
  availableKwh: number;
  totalGeneratedKwh: number;
  isVip: boolean;
};

export type PanelView = {
  id: string;
  expiresAt: string;
  dailyKwh: number;
};

export type ShopItemView = {
  id: string;
  title: string;
  priceEfhc: number;
  active: boolean;
};

export type RatingRow = {
  rank: number;
  username: string;
  totalGeneratedKwh: number;
  isCurrentUser: boolean;
};

export type ReferralStat = {
  username: string;
  isActive: boolean;
};

export type TaskView = {
  id: string;
  title: string;
  rewardEfhc: number;
  status: "open" | "submitted" | "approved" | "rejected";
};

export type AdsBannerView = {
  id: string;
  title: string;
  cta: string;
};

export type AdminMetric = {
  label: string;
  value: number;
};

const DEFAULT_BASE_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8000";

async function safeJson<T>(response: Response, fallback: T): Promise<T> {
  try {
    const data = (await response.json()) as T;
    return data;
  } catch (error) {
    console.warn("EFHC WebApp: fallback to safe JSON", error);
    return fallback;
  }
}

/**
 * Лёгкий API-клиент: только GET для отображения, без денежных операций.
 */
export class ApiClient {
  private readonly baseUrl: string;

  constructor(baseUrl: string = DEFAULT_BASE_URL) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
  }

  async getEnergy(): Promise<EnergySnapshot> {
    const response = await fetch(`${this.baseUrl}/api/v1/energy`, {
      method: "GET",
    });

    if (!response.ok) {
      return { availableKwh: 0, totalGeneratedKwh: 0, isVip: false };
    }

    return safeJson<EnergySnapshot>(response, {
      availableKwh: 0,
      totalGeneratedKwh: 0,
      isVip: false,
    });
  }

  async getPanels(): Promise<PanelView[]> {
    const response = await fetch(`${this.baseUrl}/api/v1/panels`, {
      method: "GET",
    });

    if (!response.ok) {
      return [];
    }

    return safeJson<PanelView[]>(response, []);
  }

  async getShop(): Promise<ShopItemView[]> {
    const response = await fetch(`${this.baseUrl}/api/v1/shop`, {
      method: "GET",
    });

    if (!response.ok) {
      return [];
    }

    return safeJson<ShopItemView[]>(response, []);
  }

  async getRating(): Promise<RatingRow[]> {
    const response = await fetch(`${this.baseUrl}/api/v1/rating`, {
      method: "GET",
    });

    if (!response.ok) {
      return [];
    }

    return safeJson<RatingRow[]>(response, []);
  }

  async getReferrals(): Promise<ReferralStat[]> {
    const response = await fetch(`${this.baseUrl}/api/v1/referrals`, {
      method: "GET",
    });

    if (!response.ok) {
      return [];
    }

    return safeJson<ReferralStat[]>(response, []);
  }

  async getTasks(): Promise<TaskView[]> {
    const response = await fetch(`${this.baseUrl}/api/v1/tasks`, {
      method: "GET",
    });

    if (!response.ok) {
      return [];
    }

    return safeJson<TaskView[]>(response, []);
  }

  async getAds(): Promise<AdsBannerView[]> {
    const response = await fetch(`${this.baseUrl}/api/v1/ads`, {
      method: "GET",
    });

    if (!response.ok) {
      return [];
    }

    return safeJson<AdsBannerView[]>(response, []);
  }

  async getAdminMetrics(): Promise<AdminMetric[]> {
    const response = await fetch(`${this.baseUrl}/api/v1/admin/stats`, {
      method: "GET",
    });

    if (!response.ok) {
      return [];
    }

    return safeJson<AdminMetric[]>(response, []);
  }
}

export function getApiClient(): ApiClient {
  return new ApiClient();
}

// ======================================================================
// Пояснения «для чайника»:
//   • Этот клиент только читает данные: денежные POST он не выполняет.
//   • При ошибке сети вернёт пустые списки/нулевые значения без краша UI.
//   • Балансы не пересчитываются на клиенте — числа показываются как есть.
//   • Idempotency-Key и банковские операции остаются на стороне бэкенда.
// ======================================================================
