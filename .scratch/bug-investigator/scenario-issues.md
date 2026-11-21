# Issue для своих сценариев (resourceBooking)

Черновики issue для наборов `bugset-1` и `bugset-2`. Пишутся по наблюдаемому поведению, без указания места в коде.
Создавать в `angryelizar/resourceBooking` перед прогоном соответствующего набора.

Шпаргалка (не для issue):

| Набор | Тип | Где | Что |
| --- | --- | --- | --- |
| bugset-1 | явный | `BookingServiceImpl.isOverlapLocalDateTime` | `> 0` → `>= 0`, стык броней считается пересечением; падает `bookingRightAfterConfirmedOneIsAllowed` |
| bugset-1 | тихий, тест возможен | `ResourceServiceImpl.update` | не обновляется `hourlyRate` |
| bugset-1 | тихий, без теста | `FakePaymentServiceImpl.makePayment` | реквизиты оплаты пишутся в лог |
| bugset-2 | явный | `BookingServiceImpl.getAmountForBooking` | `SECONDS` → `MINUTES`, сумма в 60 раз меньше; падают 3 теста на сумму |
| bugset-2 | тихий, тест возможен | `UserServiceImpl.create` | при регистрации выдаётся роль ADMIN |
| bugset-2 | тихий, без теста | `FakePaymentServiceImpl.makePayment` | проверка «уже оплачено» через `paymentRepository.findAll()` |
| bugset-3 | явный | `ResourceServiceImpl.delete` | ресурс удаляется раньше его броней; падает `deletingResourceDeletesItsBookings` |
| bugset-3 | тихий, тест возможен | `BookingRepository.findByIsConfirmedAndResource` | не ловит бронь целиком внутри подтверждённой |
| bugset-3 | тихий, без теста | `PayPalPaymentStrategy.pay` | состояние в полях синглтона, гонка при параллельных оплатах |
| bugset-4 | явный | `PaymentServiceImpl.executePayment` | `toUpperCase()` при поиске стратегии; падает `payingOwnBookingByCardConfirmsIt` |
| bugset-4 | тихий, тест возможен | `BookingServiceImpl.deleteBookingByUser` | сравниваются роли, а не авторы — USER удаляет чужие брони |
| bugset-4 | тихий, без теста | `ResourceServiceImpl.findAll` | пагинация в памяти после `findAll()` |

---

## bugset-1

### 1. Нельзя забронировать слот сразу после чужой брони — [#1](https://github.com/angryelizar/resourceBooking/issues/1)

Есть подтверждённая бронь зала ololoPlanet с 10:00 до 12:00. Пытаюсь забронировать тот же зал с 12:00 до 14:00
(`POST /bookings/resources/2`) — получаю 400:

```
Бронирование недоступно, есть перекрытие по другой брони либо вы пытаетесь забронировать слишком заранее :)
```

Слоты не пересекаются, одна бронь заканчивается ровно тогда, когда начинается другая. Раньше так можно было.

> Для сценария 1 вместо этого текста можно вставить вывод упавшего теста из `docker build`.

### 2. После изменения ресурса цена за час остаётся старой — [#2](https://github.com/angryelizar/resourceBooking/issues/2)

Администратор меняет у ресурса `hourlyRate` со 100 на 300 через `PUT /resources/{id}`. Ответ 200, название и описание
обновились, но `hourlyRate` в ответе и при следующем `GET` — по-прежнему 100. Новые брони считаются по старой цене.

### 3. В логах приложения видны номера карт — [#3](https://github.com/angryelizar/resourceBooking/issues/3)

В `logs/app.log` после оплаты брони картой появляется строка с полным номером карты, которую ввёл пользователь.
Реквизиты оплаты не должны попадать в логи.

---

## bugset-2

### 4. Сумма к оплате в десятки раз меньше нужной — [#4](https://github.com/angryelizar/resourceBooking/issues/4)

Бронирую зал ololoPlanet (100 за час) на 2 часа. В ответе `amountForPay` = 3.33 вместо 200. Оплата проходит
на ту же заниженную сумму.

### 5. Новый пользователь после регистрации может управлять ресурсами — [#5](https://github.com/angryelizar/resourceBooking/issues/5)

Зарегистрировался через `POST /registration` обычным пользователем, после этого смог создать ресурс через
`POST /resources`, хотя это доступно только администраторам. При этом бронировать ресурсы (`POST /bookings/resources/{id}`)
не могу — 403.

### 6. Оплата бронирования работает всё медленнее — [#6](https://github.com/angryelizar/resourceBooking/issues/6)

На проде оплата брони (`POST /payments/bookings/{id}`) стала занимать несколько секунд, и время растёт вместе
с количеством платежей в системе. В логах SQL при каждой оплате видно выборку всей таблицы `payments`.
