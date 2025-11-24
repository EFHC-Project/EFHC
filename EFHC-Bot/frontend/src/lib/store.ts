/**
======================================================================
== EFHC Bot WebApp — лёгкий стор состояния                            ==
======================================================================
Назначение: минимальный observable store для локальных UI-состояний без вмешательства в деньги.
Инварианты: хранит только отображаемые данные; не изменяет балансы и не инициирует сетевые операции.
ИИ-защита: отсутствие внешних зависимостей, безопасные подписки без утечек.
Запреты: не кэширует финансовые операции, не генерирует Idempotency-Key.
======================================================================
*/
export type Listener<T> = (state: T) => void;

export class Store<T> {
  private state: T;

  private listeners: Set<Listener<T>> = new Set();

  constructor(initialState: T) {
    this.state = initialState;
  }

  getState(): T {
    return this.state;
  }

  setState(next: T): void {
    this.state = next;
    this.listeners.forEach((listener) => listener(next));
  }

  subscribe(listener: Listener<T>): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
}

export function createStore<T>(initialState: T): Store<T> {
  return new Store<T>(initialState);
}

// ======================================================================
// Пояснения «для чайника»:
//   • Store хранит только UI-данные (например, выбранную вкладку).
//   • Денежные операции и расчёты остаются на бэкенде EFHC.
//   • Подписка безопасна: вернёт функцию для отписки, утечек нет.
// ======================================================================
