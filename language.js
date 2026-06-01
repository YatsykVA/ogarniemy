(function () {
  const dictionary = {
    pl: {
      "ogarniemy.pro | мы все уладим": "ogarniemy.pro | ogarniemy wszystko",
      "Идея": "Idea",
      "Задачи": "Sprawy",
      "Клиент": "Klient",
      "Сотрудник": "Wykonawca",
      "Процесс": "Proces",
      "Запуск": "Start",
      "Нужно решить задачу? Мы все уладим.": "Masz sprawę do załatwienia? Ogarniemy.",
      "ogarniemy.pro - это городской помощник для поручений, доставок, поездок и срочных ситуаций. Клиент пишет, что нужно сделать, а система превращает просьбу в понятную задачу для сотрудника: с адресом, контактом, ценой и результатом.": "ogarniemy.pro to miejski pomocnik do zleceń, dostaw, przejazdów i pilnych sytuacji. Klient pisze, co trzeba zrobić, a prośba zmienia się w jasne zadanie dla wykonawcy: z adresem, kontaktem, ceną i oczekiwanym wynikiem.",
      "Ты просишь. Мы улаживаем.": "Ty mówisz. My ogarniamy.",
      "Ты говоришь. Мы улаживаем.": "Ty mówisz. My ogarniamy.",
      "Одна просьба. Много возможностей. Спокойная голова.": "Jedna prośba. Wiele możliwości. Spokojna głowa.",
      "Не ищи помощь по чатам. Просто напиши, что нужно сделать.": "Nie szukaj pomocy. Po prostu napisz, co trzeba zrobić.",
      "Больше понятных заказов. Меньше хаоса. Спокойнее работа.": "Więcej jasnych zleceń. Mniej chaosu. Lepsza praca.",
      "От просьбы до результата - без потерянных сообщений.": "Od prośby do wykonania - bez zgubionych wiadomości.",
      "Маленькие дела тоже заслуживают нормального сервиса.": "Małe sprawy też zasługują na profesjonalną obsługę.",
      "Что можно поручить": "Co można zlecić",
      "Для клиентов": "Dla klientów",
      "городской сервис “под рукой”": "miejski serwis pod ręką",
      "Не искать случайного человека. Не объяснять одно и то же пять раз. Не держать задачу в голове. Просто оставить поручение и получить помощь.": "Nie szukać przypadkowej osoby. Nie tłumaczyć tego samego pięć razy. Nie trzymać sprawy w głowie. Po prostu dodać zlecenie i dostać pomoc.",
      "ogarniemy.pro - это не “одна услуга”. Это городской помощник для всего, что нужно решить руками, временем, поездкой или ответственным человеком.": "ogarniemy.pro to nie “jedna usługa”. To miejski pomocnik do wszystkiego, co trzeba rozwiązać rękami, czasem, przejazdem albo odpowiedzialną osobą.",
      "быстро": "szybko",
      "по адресу": "pod adres",
      "понятно": "jasno",
      "широкий формат": "szeroki format",
      "Это не одна услуга. Это способ закрывать любые мелкие и срочные дела.": "To nie jest jedna usługa. To sposób na zamykanie małych i pilnych spraw.",
      "У людей постоянно появляются задачи, ради которых неудобно искать отдельного специалиста: что-то забрать, отвезти, купить, донести, проверить, сфотографировать, сопроводить или помочь на месте. ogarniemy.pro делает такие просьбы нормальным сервисом.": "Ludzie ciągle mają sprawy, dla których trudno szukać osobnego specjalisty: coś odebrać, zawieźć, kupić, donieść, sprawdzić, sfotografować, odprowadzić albo pomóc na miejscu. ogarniemy.pro robi z takich próśb normalną usługę.",
      "Доставить": "Dostarczyć",
      "документы, покупки, вещи, посылку, ключи, оборудование или забытый предмет.": "dokumenty, zakupy, rzeczy, paczkę, klucze, sprzęt albo zapomniany przedmiot.",
      "Подвезти": "Podwieźć",
      "человека, коробки, инструмент, товар, подарок или что-то срочное по городу.": "osobę, kartony, narzędzia, towar, prezent albo coś pilnego po mieście.",
      "Купить и привезти": "Kupić i przywieźć",
      "лекарства, продукты, расходники, детали, цветы, подарок или нужную мелочь.": "leki, produkty, materiały, części, kwiaty, prezent albo potrzebną drobnostkę.",
      "Помочь на месте": "Pomóc na miejscu",
      "накачать колесо, донести, встретить, подождать, проверить адрес, сделать фото, решить вопрос.": "dopompować koło, donieść, spotkać, poczekać, sprawdzić adres, zrobić zdjęcie, rozwiązać sprawę.",
      "Живые сценарии": "Realne scenariusze",
      "срочно": "pilne",
      "Забрать документы и отвезти клиенту": "Odebrać dokumenty i zawieźć klientowi",
      "В карточке уже есть адрес, телефон, время и сумма.": "W karcie jest już adres, telefon, czas i kwota.",
      "город": "miasto",
      "Купить деталь и привезти мастеру": "Kupić część i przywieźć fachowcowi",
      "Клиенту не нужно искать транспорт и объяснять маршрут заново.": "Klient nie musi szukać transportu i tłumaczyć trasy od nowa.",
      "помощь": "pomoc",
      "Накачать колесо и сопроводить до сервиса": "Dopompować koło i podprowadzić do serwisu",
      "Ситуация решается как поручение, а не как паника.": "Sytuacja staje się zadaniem, a nie paniką.",
      "просьба": "prośba",
      "исполнитель": "wykonawca",
      "хаоса": "chaosu",
      "Новая задача": "Nowe zadanie",
      "Что нужно сделать": "Co trzeba zrobić",
      "Забрать и привезти": "Odebrać i przywieźć",
      "Описание": "Opis",
      "Нужно забрать коробки, позвонить на месте и привезти по адресу.": "Trzeba odebrać kartony, zadzwonić na miejscu i przywieźć pod adres.",
      "Телефон для связи": "Telefon kontaktowy",
      "Адрес или точка встречи": "Adres albo punkt spotkania",
      "Карта": "Karta",
      "Наличные": "Gotówka",
      "Отправить задачу": "Wyślij zadanie",
      "Мои задачи": "Moje zadania",
      "К оплате": "Do zapłaty",
      "Резерв": "Rezerwa",
      "Забрать коробки": "Odebrać kartony",
      "Принято сотрудником": "Przyjęte przez wykonawcę",
      "Статус: в работе": "Status: w pracy",
      "Купить и доставить": "Kupić i dostarczyć",
      "Выполнено": "Wykonane",
      "Ожидает расчет": "Czeka na rozliczenie",
      "почему клиенту удобно": "dlaczego klientowi jest wygodnie",
      "Клиенту не нужно искать, кому доверить задачу.": "Klient nie musi szukać, komu powierzyć sprawę.",
      "В обычной жизни мелкая просьба быстро превращается в цепочку звонков: кто может, когда, сколько стоит, куда ехать, кому звонить. В ogarniemy.pro это становится одной понятной заявкой. Человек пишет задачу, добавляет детали и видит, что процесс начался.": "W zwykłym życiu drobna prośba szybko zmienia się w serię telefonów: kto może, kiedy, ile kosztuje, dokąd jechać, do kogo dzwonić. W ogarniemy.pro staje się jedną jasną sprawą. Klient wpisuje zadanie, dodaje szczegóły i widzi, że proces ruszył.",
      "не нужно разбираться в категориях: можно описать задачу обычными словами;": "nie trzeba wybierać kategorii: zadanie można opisać zwykłymi słowami;",
      "все детали в одном месте: адрес, телефон, описание, цена и способ оплаты;": "wszystkie szczegóły są w jednym miejscu: adres, telefon, opis, cena i płatność;",
      "подходит для срочных ситуаций, когда помощь нужна сегодня или прямо сейчас;": "pasuje do pilnych sytuacji, gdy pomoc jest potrzebna dziś albo natychmiast;",
      "задача не теряется в переписках: у нее есть ответственный и понятный статус;": "zadanie nie ginie w wiadomościach: ma odpowiedzialną osobę i jasny status;",
      "клиент получает спокойствие: просьба принята, человек назначен, результат ожидаем.": "klient dostaje spokój: prośba przyjęta, osoba wyznaczona, wynik jest oczekiwany.",
      "почему сотруднику выгодно": "dlaczego wykonawcy się opłaca",
      "Сотрудник видит понятные заказы, а не обрывки переписок.": "Wykonawca widzi jasne zlecenia, a nie urywki rozmów.",
      "Приложение превращает работу в спокойный список задач. В карточке уже есть суть поручения, адрес, контакт, цена, способ оплаты и действие, которое нужно сделать. Сотрудник быстро понимает, подходит ли ему заказ, берет его и закрывает по факту выполнения.": "Aplikacja zamienia pracę w spokojną listę zadań. W karcie jest sens zlecenia, adres, kontakt, cena, płatność i działanie do wykonania. Wykonawca szybko rozumie, czy zlecenie mu pasuje, bierze je i zamyka po wykonaniu.",
      "Удобно": "Wygodnie",
      "Меньше лишних звонков, меньше недопонимания, меньше “а пришлите адрес еще раз”. Все важное собрано в одном экране.": "Mniej zbędnych telefonów, mniej nieporozumień, mniej próśb o ponowne wysłanie adresu. Wszystko ważne jest na jednym ekranie.",
      "Прозрачно": "Przejrzyście",
      "Видны выполненные задачи, расчет, сумма к выплате и история. Сотрудник понимает, за что он получает деньги.": "Widać wykonane zadania, rozliczenie, kwotę do wypłaty i historię. Wykonawca wie, za co dostaje pieniądze.",
      "К выплате": "Do wypłaty",
      "Доступные задачи": "Dostępne zadania",
      "Накачать колесо": "Dopompować koło",
      "Адрес, телефон, цена, оплата": "Adres, telefon, cena, płatność",
      "Принять": "Przyjmij",
      "Отказ": "Odmowa",
      "Доставка пакета": "Dostawa paczki",
      "В работе • принято 12:40": "W pracy • przyjęto 12:40",
      "Готово": "Gotowe",
      "как это работает": "jak to działa",
      "Простой путь: попросил, приняли, сделали, рассчитались.": "Prosta droga: poprosił, przyjęli, zrobili, rozliczyli.",
      "Сервис должен ощущаться как надежный человек “под рукой”, только с нормальной организацией. Клиент не держит задачу в голове, сотрудник не теряет детали, компания видит качество и деньги.": "Serwis ma działać jak zaufana osoba pod ręką, tylko z dobrą organizacją. Klient nie trzyma sprawy w głowie, wykonawca nie traci szczegółów, firma widzi jakość i pieniądze.",
      "Клиент оставляет просьбу": "Klient zostawia prośbę",
      "Через приложение, сайт, мессенджер или звонок. Формулировка может быть простой: “заберите”, “отвезите”, “помогите”, “нужно срочно”.": "Przez aplikację, stronę, komunikator albo telefon. Treść może być prosta: “odbierzcie”, “zawieźcie”, “pomóżcie”, “pilne”.",
      "Задача становится ясной": "Zadanie staje się jasne",
      "Добавляются адрес, контакт, цена, способ оплаты, время и важные детали. Исполнитель получает нормальную карточку, а не набор сообщений.": "Dodaje się adres, kontakt, cenę, płatność, czas i ważne szczegóły. Wykonawca dostaje normalną kartę, a nie zestaw wiadomości.",
      "Сотрудник берет в работу": "Wykonawca bierze zadanie",
      "Он принимает задачу, видит всю информацию и выполняет ее. Клиент понимает, что поручение уже не висит в воздухе.": "Przyjmuje zadanie, widzi wszystkie informacje i je wykonuje. Klient wie, że sprawa nie wisi w powietrzu.",
      "Все закрывается честно": "Wszystko zamyka się uczciwie",
      "Статус меняется, сумма попадает в расчет, история сохраняется. Это удобно клиенту, сотруднику и компании.": "Status się zmienia, kwota trafia do rozliczenia, historia zostaje. To wygodne dla klienta, wykonawcy i firmy.",
      "почему это может выстрелить": "dlaczego to może zadziałać",
      "Людям постоянно нужна помощь, но мелкие задачи до сих пор решаются слишком хаотично.": "Ludzie stale potrzebują pomocy, ale małe sprawy nadal rozwiązuje się zbyt chaotycznie.",
      "У каждого бывают ситуации, когда нет времени, машины, инструмента, знакомого человека или просто сил. ogarniemy.pro может стать привычной кнопкой помощи: открыл, написал задачу, получил результат.": "Każdy ma sytuacje, gdy brakuje czasu, auta, narzędzi, znajomej osoby albo sił. ogarniemy.pro może stać się zwykłym przyciskiem pomocy: otworzył, napisał sprawę, dostał wynik.",
      "сервис не ограничен одной нишей, поэтому заказов может быть много и разных;": "serwis nie ogranicza się do jednej niszy, więc zleceń może być dużo i różnych;",
      "клиенту проще доверять приложению, где есть понятный процесс и история;": "klientowi łatwiej zaufać aplikacji z jasnym procesem i historią;",
      "сотрудникам проще работать, потому что задачи собраны в одном месте;": "wykonawcom łatwiej pracować, bo zadania są w jednym miejscu;",
      "после запуска можно расширять категории, города, сотрудников и партнеров;": "po starcie można rozwijać kategorie, miasta, wykonawców i partnerów;",
      "главное обещание короткое и сильное: “Ogarniemy” - мы все уладим.": "główna obietnica jest krótka i mocna: “Ogarniemy” - wszystko załatwimy.",
      "Главная идея": "Główna idea",
      "ogarniemy.pro - это не “одна услуга”. Это городской помощник для всего, что нужно решить руками, временем, поездкой или ответственным человеком. Клиент получает спокойствие, сотрудник получает заказы, компания получает управляемый сервис.": "ogarniemy.pro to nie “jedna usługa”. To miejski pomocnik do wszystkiego, co trzeba rozwiązać rękami, czasem, przejazdem albo odpowiedzialną osobą. Klient dostaje spokój, wykonawca dostaje zlecenia, firma dostaje zarządzalny serwis.",
      "Клиент получает спокойствие, сотрудник получает заказы, компания получает управляемый сервис. Чем проще задача выглядит для клиента, тем сильнее сервис внутри.": "Klient dostaje spokój, wykonawca dostaje zlecenia, firma dostaje zarządzalny serwis. Im prościej wygląda zadanie dla klienta, tym mocniejszy jest serwis w środku.",
      "Смотреть снова": "Zobacz ponownie",
      "К приложению": "Do aplikacji"
    },
    en: {},
    uk: {},
    ru: {}
  };

  dictionary.ru = Object.fromEntries(Object.keys(dictionary.pl).map((key) => [key, key]));
  dictionary.en = {
    ...dictionary.ru,
    "ogarniemy.pro | мы все уладим": "ogarniemy.pro | we will handle it",
    "Идея": "Idea", "Задачи": "Tasks", "Клиент": "Client", "Сотрудник": "Worker", "Процесс": "Process", "Запуск": "Launch",
    "Нужно решить задачу? Мы все уладим.": "Need something handled? We will take care of it.",
    "Ты просишь. Мы улаживаем.": "You ask. We handle it.",
    "Ты говоришь. Мы улаживаем.": "You say it. We handle it.",
    "Одна просьба. Много возможностей. Спокойная голова.": "One request. Many possibilities. Peace of mind.",
    "Не ищи помощь по чатам. Просто напиши, что нужно сделать.": "Do not hunt for help in chats. Just write what needs to be done.",
    "Больше понятных заказов. Меньше хаоса. Спокойнее работа.": "More clear jobs. Less chaos. Calmer work.",
    "От просьбы до результата - без потерянных сообщений.": "From request to result, without lost messages.",
    "Маленькие дела тоже заслуживают нормального сервиса.": "Small tasks deserve proper service too.",
    "Что можно поручить": "What you can request", "Для клиентов": "For clients",
    "городской сервис “под рукой”": "a city service at hand",
    "быстро": "fast", "по адресу": "to the address", "понятно": "clear",
    "широкий формат": "wide format",
    "Это не одна услуга. Это способ закрывать любые мелкие и срочные дела.": "It is not one service. It is a way to close any small or urgent task.",
    "Доставить": "Deliver", "Подвезти": "Give a ride", "Купить и привезти": "Buy and bring", "Помочь на месте": "Help on site",
    "Живые сценарии": "Real scenarios", "срочно": "urgent", "город": "city", "помощь": "help", "просьба": "request", "исполнитель": "worker", "хаоса": "chaos",
    "Новая задача": "New task", "Что нужно сделать": "What needs to be done", "Забрать и привезти": "Pick up and bring", "Описание": "Description",
    "Телефон для связи": "Contact phone", "Адрес или точка встречи": "Address or meeting point", "Карта": "Card", "Наличные": "Cash", "Отправить задачу": "Send task",
    "Мои задачи": "My tasks", "К оплате": "To pay", "Резерв": "Reserve", "Принято сотрудником": "Accepted by worker", "Статус: в работе": "Status: in progress",
    "Выполнено": "Completed", "Ожидает расчет": "Waiting for settlement",
    "почему клиенту удобно": "why it is convenient for clients",
    "Клиенту не нужно искать, кому доверить задачу.": "The client does not need to search for someone to trust.",
    "почему сотруднику выгодно": "why it works for workers",
    "Сотрудник видит понятные заказы, а не обрывки переписок.": "The worker sees clear jobs, not fragments of chats.",
    "Удобно": "Convenient", "Прозрачно": "Transparent", "К выплате": "Payout", "Доступные задачи": "Available tasks",
    "Накачать колесо": "Pump a tire", "Принять": "Accept", "Отказ": "Decline", "Доставка пакета": "Package delivery", "Готово": "Done",
    "как это работает": "how it works",
    "Простой путь: попросил, приняли, сделали, рассчитались.": "A simple flow: asked, accepted, done, settled.",
    "Клиент оставляет просьбу": "The client leaves a request", "Задача становится ясной": "The task becomes clear",
    "Сотрудник берет в работу": "The worker takes it", "Все закрывается честно": "Everything closes fairly",
    "почему это может выстрелить": "why it can work",
    "Людям постоянно нужна помощь, но мелкие задачи до сих пор решаются слишком хаотично.": "People constantly need help, but small tasks are still handled too chaotically.",
    "Главная идея": "Main idea", "Смотреть снова": "Watch again", "К приложению": "To the app"
  };
  dictionary.uk = {
    ...dictionary.ru,
    "ogarniemy.pro | мы все уладим": "ogarniemy.pro | ми все владнаємо",
    "Идея": "Ідея", "Задачи": "Завдання", "Клиент": "Клієнт", "Сотрудник": "Працівник", "Процесс": "Процес", "Запуск": "Запуск",
    "Нужно решить задачу? Мы все уладим.": "Потрібно вирішити завдання? Ми все владнаємо.",
    "Ты просишь. Мы улаживаем.": "Ти просиш. Ми владнаємо.",
    "Ты говоришь. Мы улаживаем.": "Ти кажеш. Ми владнаємо.",
    "Одна просьба. Много возможностей. Спокойная голова.": "Одне прохання. Багато можливостей. Спокійна голова.",
    "Не ищи помощь по чатам. Просто напиши, что нужно сделать.": "Не шукай допомогу в чатах. Просто напиши, що потрібно зробити.",
    "Больше понятных заказов. Меньше хаоса. Спокойнее работа.": "Більше зрозумілих замовлень. Менше хаосу. Спокійніша робота.",
    "От просьбы до результата - без потерянных сообщений.": "Від прохання до результату - без загублених повідомлень.",
    "Маленькие дела тоже заслуживают нормального сервиса.": "Маленькі справи теж заслуговують на нормальний сервіс.",
    "Что можно поручить": "Що можна доручити", "Для клиентов": "Для клієнтів",
    "городской сервис “под рукой”": "міський сервіс під рукою",
    "быстро": "швидко", "по адресу": "за адресою", "понятно": "зрозуміло",
    "широкий формат": "широкий формат",
    "Это не одна услуга. Это способ закрывать любые мелкие и срочные дела.": "Це не одна послуга. Це спосіб закривати будь-які дрібні й термінові справи.",
    "Доставить": "Доставити", "Подвезти": "Підвезти", "Купить и привезти": "Купити й привезти", "Помочь на месте": "Допомогти на місці",
    "Живые сценарии": "Живі сценарії", "срочно": "терміново", "город": "місто", "помощь": "допомога", "просьба": "прохання", "исполнитель": "виконавець", "хаоса": "хаосу",
    "Новая задача": "Нове завдання", "Что нужно сделать": "Що потрібно зробити", "Забрать и привезти": "Забрати й привезти", "Описание": "Опис",
    "Телефон для связи": "Телефон для зв'язку", "Адрес или точка встречи": "Адреса або місце зустрічі", "Карта": "Картка", "Наличные": "Готівка", "Отправить задачу": "Надіслати завдання",
    "Мои задачи": "Мої завдання", "К оплате": "До оплати", "Резерв": "Резерв", "Принято сотрудником": "Прийнято працівником", "Статус: в работе": "Статус: у роботі",
    "Выполнено": "Виконано", "Ожидает расчет": "Очікує розрахунок",
    "почему клиенту удобно": "чому клієнту зручно",
    "Клиенту не нужно искать, кому доверить задачу.": "Клієнту не потрібно шукати, кому довірити завдання.",
    "почему сотруднику выгодно": "чому працівнику вигідно",
    "Сотрудник видит понятные заказы, а не обрывки переписок.": "Працівник бачить зрозумілі замовлення, а не уривки переписок.",
    "Удобно": "Зручно", "Прозрачно": "Прозоро", "К выплате": "До виплати", "Доступные задачи": "Доступні завдання",
    "Накачать колесо": "Накачати колесо", "Принять": "Прийняти", "Отказ": "Відмова", "Доставка пакета": "Доставка пакета", "Готово": "Готово",
    "как это работает": "як це працює",
    "Простой путь: попросил, приняли, сделали, рассчитались.": "Простий шлях: попросив, прийняли, зробили, розрахувались.",
    "Клиент оставляет просьбу": "Клієнт залишає прохання", "Задача становится ясной": "Завдання стає зрозумілим",
    "Сотрудник берет в работу": "Працівник бере в роботу", "Все закрывается честно": "Усе закривається чесно",
    "почему это может выстрелить": "чому це може спрацювати",
    "Людям постоянно нужна помощь, но мелкие задачи до сих пор решаются слишком хаотично.": "Людям постійно потрібна допомога, але дрібні завдання досі вирішуються надто хаотично.",
    "Главная идея": "Головна ідея", "Смотреть снова": "Дивитися знову", "К приложению": "До застосунку"
  };

  Object.assign(dictionary.en, {
    "ogarniemy.pro - это городской помощник для поручений, доставок, поездок и срочных ситуаций. Клиент пишет, что нужно сделать, а система превращает просьбу в понятную задачу для сотрудника: с адресом, контактом, ценой и результатом.": "ogarniemy.pro is a city helper for errands, deliveries, rides and urgent situations. The client writes what needs to be done, and the request becomes a clear task for a worker: with an address, contact, price and expected result.",
    "Не искать случайного человека. Не объяснять одно и то же пять раз. Не держать задачу в голове. Просто оставить поручение и получить помощь.": "No need to search for a random person. No need to explain the same thing five times. No need to keep the task in your head. Just leave a request and get help.",
    "ogarniemy.pro - это не “одна услуга”. Это городской помощник для всего, что нужно решить руками, временем, поездкой или ответственным человеком.": "ogarniemy.pro is not “one service”. It is a city helper for anything that needs to be solved with hands, time, a trip or a responsible person.",
    "Клиент получает спокойствие, сотрудник получает заказы, компания получает управляемый сервис. Чем проще задача выглядит для клиента, тем сильнее сервис внутри.": "The client gets peace of mind, the worker gets jobs, and the company gets a manageable service. The simpler the task looks to the client, the stronger the service is inside.",
    "У людей постоянно появляются задачи, ради которых неудобно искать отдельного специалиста: что-то забрать, отвезти, купить, донести, проверить, сфотографировать, сопроводить или помочь на месте. ogarniemy.pro делает такие просьбы нормальным сервисом.": "People constantly have tasks where searching for a separate specialist feels excessive: pick something up, drive it somewhere, buy it, carry it, check it, photograph it, accompany someone or help on site. ogarniemy.pro turns these requests into a normal service.",
    "документы, покупки, вещи, посылку, ключи, оборудование или забытый предмет.": "documents, shopping, belongings, a parcel, keys, equipment or a forgotten item.",
    "человека, коробки, инструмент, товар, подарок или что-то срочное по городу.": "a person, boxes, tools, goods, a gift or something urgent across the city.",
    "лекарства, продукты, расходники, детали, цветы, подарок или нужную мелочь.": "medicine, groceries, supplies, parts, flowers, a gift or a small needed item.",
    "накачать колесо, донести, встретить, подождать, проверить адрес, сделать фото, решить вопрос.": "pump a tire, carry something, meet someone, wait, check an address, take a photo, solve an issue.",
    "Забрать документы и отвезти клиенту": "Pick up documents and deliver them to a client",
    "В карточке уже есть адрес, телефон, время и сумма.": "The card already contains the address, phone, time and amount.",
    "Купить деталь и привезти мастеру": "Buy a part and bring it to the technician",
    "Клиенту не нужно искать транспорт и объяснять маршрут заново.": "The client does not need to find transport and explain the route again.",
    "Накачать колесо и сопроводить до сервиса": "Pump a tire and accompany the person to a service station",
    "Ситуация решается как поручение, а не как паника.": "The situation is handled as a task, not as panic.",
    "Нужно забрать коробки, позвонить на месте и привезти по адресу.": "Pick up the boxes, call on arrival and deliver them to the address.",
    "Забрать коробки": "Pick up boxes",
    "Купить и доставить": "Buy and deliver",
    "В обычной жизни мелкая просьба быстро превращается в цепочку звонков: кто может, когда, сколько стоит, куда ехать, кому звонить. В ogarniemy.pro это становится одной понятной заявкой. Человек пишет задачу, добавляет детали и видит, что процесс начался.": "In everyday life, a small request quickly becomes a chain of calls: who can do it, when, how much, where to go, who to call. In ogarniemy.pro it becomes one clear request. The person writes the task, adds the details and sees that the process has started.",
    "не нужно разбираться в категориях: можно описать задачу обычными словами;": "no need to understand categories: the task can be described in normal words;",
    "все детали в одном месте: адрес, телефон, описание, цена и способ оплаты;": "all details are in one place: address, phone, description, price and payment method;",
    "подходит для срочных ситуаций, когда помощь нужна сегодня или прямо сейчас;": "works for urgent situations when help is needed today or right now;",
    "задача не теряется в переписках: у нее есть ответственный и понятный статус;": "the task does not get lost in chats: it has a responsible person and a clear status;",
    "клиент получает спокойствие: просьба принята, человек назначен, результат ожидаем.": "the client gets peace of mind: the request is accepted, a person is assigned, the result is expected.",
    "Приложение превращает работу в спокойный список задач. В карточке уже есть суть поручения, адрес, контакт, цена, способ оплаты и действие, которое нужно сделать. Сотрудник быстро понимает, подходит ли ему заказ, берет его и закрывает по факту выполнения.": "The app turns work into a calm task list. The card already contains the request, address, contact, price, payment method and action to perform. The worker quickly understands whether the job fits, takes it and closes it after completion.",
    "Меньше лишних звонков, меньше недопонимания, меньше “а пришлите адрес еще раз”. Все важное собрано в одном экране.": "Fewer unnecessary calls, fewer misunderstandings, fewer “please send the address again” moments. Everything important is on one screen.",
    "Видны выполненные задачи, расчет, сумма к выплате и история. Сотрудник понимает, за что он получает деньги.": "Completed tasks, settlement, payout amount and history are visible. The worker understands what they are paid for.",
    "Адрес, телефон, цена, оплата": "Address, phone, price, payment",
    "В работе • принято 12:40": "In progress • accepted 12:40",
    "Сервис должен ощущаться как надежный человек “под рукой”, только с нормальной организацией. Клиент не держит задачу в голове, сотрудник не теряет детали, компания видит качество и деньги.": "The service should feel like a reliable person at hand, but with proper organization. The client does not keep the task in their head, the worker does not lose details, and the company sees quality and money.",
    "Через приложение, сайт, мессенджер или звонок. Формулировка может быть простой: “заберите”, “отвезите”, “помогите”, “нужно срочно”.": "Through the app, website, messenger or a call. The wording can be simple: “pick it up”, “deliver it”, “help”, “urgent”.",
    "Добавляются адрес, контакт, цена, способ оплаты, время и важные детали. Исполнитель получает нормальную карточку, а не набор сообщений.": "Address, contact, price, payment method, time and important details are added. The worker gets a proper card, not a pile of messages.",
    "Он принимает задачу, видит всю информацию и выполняет ее. Клиент понимает, что поручение уже не висит в воздухе.": "They accept the task, see all information and complete it. The client understands the request is no longer hanging in the air.",
    "Статус меняется, сумма попадает в расчет, история сохраняется. Это удобно клиенту, сотруднику и компании.": "The status changes, the amount goes into settlement, and the history is saved. It is convenient for the client, worker and company.",
    "У каждого бывают ситуации, когда нет времени, машины, инструмента, знакомого человека или просто сил. ogarniemy.pro может стать привычной кнопкой помощи: открыл, написал задачу, получил результат.": "Everyone has situations with no time, car, tool, familiar person or simply no energy. ogarniemy.pro can become the usual help button: open it, write the task, get the result.",
    "сервис не ограничен одной нишей, поэтому заказов может быть много и разных;": "the service is not limited to one niche, so tasks can be many and varied;",
    "клиенту проще доверять приложению, где есть понятный процесс и история;": "it is easier for clients to trust an app with a clear process and history;",
    "сотрудникам проще работать, потому что задачи собраны в одном месте;": "workers can work more easily because tasks are collected in one place;",
    "после запуска можно расширять категории, города, сотрудников и партнеров;": "after launch, categories, cities, workers and partners can be expanded;",
    "главное обещание короткое и сильное: “Ogarniemy” - мы все уладим.": "the main promise is short and strong: “Ogarniemy” - we will handle it."
  });

  Object.assign(dictionary.uk, {
    "ogarniemy.pro - это городской помощник для поручений, доставок, поездок и срочных ситуаций. Клиент пишет, что нужно сделать, а система превращает просьбу в понятную задачу для сотрудника: с адресом, контактом, ценой и результатом.": "ogarniemy.pro - це міський помічник для доручень, доставок, поїздок і термінових ситуацій. Клієнт пише, що потрібно зробити, а прохання перетворюється на зрозуміле завдання для працівника: з адресою, контактом, ціною і результатом.",
    "Не искать случайного человека. Не объяснять одно и то же пять раз. Не держать задачу в голове. Просто оставить поручение и получить помощь.": "Не шукати випадкову людину. Не пояснювати одне й те саме п'ять разів. Не тримати завдання в голові. Просто залишити доручення і отримати допомогу.",
    "ogarniemy.pro - это не “одна услуга”. Это городской помощник для всего, что нужно решить руками, временем, поездкой или ответственным человеком.": "ogarniemy.pro - це не “одна послуга”. Це міський помічник для всього, що потрібно вирішити руками, часом, поїздкою або відповідальною людиною.",
    "Клиент получает спокойствие, сотрудник получает заказы, компания получает управляемый сервис. Чем проще задача выглядит для клиента, тем сильнее сервис внутри.": "Клієнт отримує спокій, працівник отримує замовлення, компанія отримує керований сервіс. Чим простішим завдання виглядає для клієнта, тим сильніший сервіс всередині.",
    "У людей постоянно появляются задачи, ради которых неудобно искать отдельного специалиста: что-то забрать, отвезти, купить, донести, проверить, сфотографировать, сопроводить или помочь на месте. ogarniemy.pro делает такие просьбы нормальным сервисом.": "У людей постійно з'являються завдання, заради яких незручно шукати окремого спеціаліста: щось забрати, відвезти, купити, донести, перевірити, сфотографувати, супроводити або допомогти на місці. ogarniemy.pro робить такі прохання нормальним сервісом.",
    "документы, покупки, вещи, посылку, ключи, оборудование или забытый предмет.": "документи, покупки, речі, посилку, ключі, обладнання або забутий предмет.",
    "человека, коробки, инструмент, товар, подарок или что-то срочное по городу.": "людину, коробки, інструмент, товар, подарунок або щось термінове містом.",
    "лекарства, продукты, расходники, детали, цветы, подарок или нужную мелочь.": "ліки, продукти, витратні матеріали, деталі, квіти, подарунок або потрібну дрібницю.",
    "накачать колесо, донести, встретить, подождать, проверить адрес, сделать фото, решить вопрос.": "накачати колесо, донести, зустріти, почекати, перевірити адресу, зробити фото, вирішити питання.",
    "В карточке уже есть адрес, телефон, время и сумма.": "У картці вже є адреса, телефон, час і сума.",
    "Клиенту не нужно искать транспорт и объяснять маршрут заново.": "Клієнту не потрібно шукати транспорт і пояснювати маршрут заново.",
    "Ситуация решается как поручение, а не как паника.": "Ситуація вирішується як доручення, а не як паніка.",
    "В обычной жизни мелкая просьба быстро превращается в цепочку звонков: кто может, когда, сколько стоит, куда ехать, кому звонить. В ogarniemy.pro это становится одной понятной заявкой. Человек пишет задачу, добавляет детали и видит, что процесс начался.": "У звичайному житті дрібне прохання швидко перетворюється на ланцюжок дзвінків: хто може, коли, скільки коштує, куди їхати, кому дзвонити. В ogarniemy.pro це стає однією зрозумілою заявкою. Людина пише завдання, додає деталі й бачить, що процес почався.",
    "Приложение превращает работу в спокойный список задач. В карточке уже есть суть поручения, адрес, контакт, цена, способ оплаты и действие, которое нужно сделать. Сотрудник быстро понимает, подходит ли ему заказ, берет его и закрывает по факту выполнения.": "Застосунок перетворює роботу на спокійний список завдань. У картці вже є суть доручення, адреса, контакт, ціна, спосіб оплати й дія, яку потрібно зробити. Працівник швидко розуміє, чи підходить йому замовлення, бере його і закриває після виконання.",
    "Сервис должен ощущаться как надежный человек “под рукой”, только с нормальной организацией. Клиент не держит задачу в голове, сотрудник не теряет детали, компания видит качество и деньги.": "Сервіс має відчуватися як надійна людина під рукою, тільки з нормальною організацією. Клієнт не тримає завдання в голові, працівник не губить деталі, компанія бачить якість і гроші.",
    "У каждого бывают ситуации, когда нет времени, машины, инструмента, знакомого человека или просто сил. ogarniemy.pro может стать привычной кнопкой помощи: открыл, написал задачу, получил результат.": "У кожного бувають ситуації, коли немає часу, машини, інструмента, знайомої людини або просто сил. ogarniemy.pro може стати звичною кнопкою допомоги: відкрив, написав завдання, отримав результат.",
    "главное обещание короткое и сильное: “Ogarniemy” - мы все уладим.": "головна обіцянка коротка й сильна: “Ogarniemy” - ми все владнаємо."
  });

  Object.assign(dictionary.pl, {
    "быстро": "szybko",
    "надежно": "niezawodnie",
    "почему клиенту удобно?": "dlaczego klientowi jest wygodnie?",
    "почему сотруднику выгодно?": "dlaczego wykonawcy się opłaca?",
    "как это работает?": "jak to działa?",
    "почему мы?": "dlaczego my?",
    "Я клиент": "Jestem klientem",
    "Я сотрудник": "Jestem wykonawcą"
  });

  Object.assign(dictionary.en, {
    "быстро": "fast",
    "надежно": "reliable",
    "почему клиенту удобно?": "why is it convenient for clients?",
    "почему сотруднику выгодно?": "why does it work for workers?",
    "как это работает?": "how does it work?",
    "почему мы?": "why us?",
    "Я клиент": "I am a client",
    "Я сотрудник": "I am a worker"
  });

  Object.assign(dictionary.uk, {
    "быстро": "швидко",
    "надежно": "надійно",
    "почему клиенту удобно?": "чому клієнту зручно?",
    "почему сотруднику выгодно?": "чому працівнику вигідно?",
    "как это работает?": "як це працює?",
    "почему мы?": "чому ми?",
    "Я клиент": "Я клієнт",
    "Я сотрудник": "Я працівник"
  });

  Object.assign(dictionary.pl, {
    "Регистрация клиента": "Rejestracja klienta",
    "Регистрация сотрудника": "Rejestracja wykonawcy",
    "Создайте аккаунт и скачайте приложение": "Załóż konto i pobierz aplikację",
    "Имя клиента": "Imię klienta",
    "Имя сотрудника": "Imię wykonawcy",
    "Номер телефона": "Numer telefonu",
    "Пароль": "Hasło",
    "Зарегистрироваться": "Zarejestruj się",
    "Приложение клиента для Android": "Aplikacja klienta na Androida",
    "Приложение сотрудника для Android": "Aplikacja wykonawcy na Androida",
    "Приложение клиента для iPhone": "Aplikacja klienta na iPhone'a",
    "Приложение сотрудника для iPhone": "Aplikacja wykonawcy na iPhone'a",
    "Отсканируйте QR-код или скачайте приложение напрямую.": "Zeskanuj kod QR albo pobierz aplikację bezpośrednio.",
    "Скачать для Android": "Pobierz na Androida",
    "Версия для iPhone готовится. QR-код ведет к актуальной информации на сайте.": "Wersja na iPhone'a jest przygotowywana. Kod QR prowadzi do aktualnych informacji na stronie.",
    "Скоро для iPhone": "Wkrótce na iPhone'a"
  });

  Object.assign(dictionary.en, {
    "Регистрация клиента": "Client registration",
    "Регистрация сотрудника": "Worker registration",
    "Создайте аккаунт и скачайте приложение": "Create an account and download the app",
    "Имя клиента": "Client name",
    "Имя сотрудника": "Worker name",
    "Номер телефона": "Phone number",
    "Пароль": "Password",
    "Зарегистрироваться": "Register",
    "Приложение клиента для Android": "Client app for Android",
    "Приложение сотрудника для Android": "Worker app for Android",
    "Приложение клиента для iPhone": "Client app for iPhone",
    "Приложение сотрудника для iPhone": "Worker app for iPhone",
    "Отсканируйте QR-код или скачайте приложение напрямую.": "Scan the QR code or download the app directly.",
    "Скачать для Android": "Download for Android",
    "Версия для iPhone готовится. QR-код ведет к актуальной информации на сайте.": "The iPhone version is being prepared. The QR code leads to the latest information on the website.",
    "Скоро для iPhone": "Coming soon for iPhone"
  });

  Object.assign(dictionary.uk, {
    "Регистрация клиента": "Реєстрація клієнта",
    "Регистрация сотрудника": "Реєстрація працівника",
    "Создайте аккаунт и скачайте приложение": "Створіть обліковий запис і завантажте застосунок",
    "Имя клиента": "Ім'я клієнта",
    "Имя сотрудника": "Ім'я працівника",
    "Номер телефона": "Номер телефону",
    "Пароль": "Пароль",
    "Зарегистрироваться": "Зареєструватися",
    "Приложение клиента для Android": "Застосунок клієнта для Android",
    "Приложение сотрудника для Android": "Застосунок працівника для Android",
    "Приложение клиента для iPhone": "Застосунок клієнта для iPhone",
    "Приложение сотрудника для iPhone": "Застосунок працівника для iPhone",
    "Отсканируйте QR-код или скачайте приложение напрямую.": "Відскануйте QR-код або завантажте застосунок безпосередньо.",
    "Скачать для Android": "Завантажити для Android",
    "Версия для iPhone готовится. QR-код ведет к актуальной информации на сайте.": "Версія для iPhone готується. QR-код веде до актуальної інформації на сайті.",
    "Скоро для iPhone": "Незабаром для iPhone"
  });

  const originalText = new WeakMap();
  const languageSelect = document.querySelector("#presentationLanguage");

  function translateTextNode(node, language) {
    if (!originalText.has(node)) originalText.set(node, node.nodeValue);
    const source = originalText.get(node);
    const trimmed = source.trim();
    if (!trimmed) return;
    const translated = dictionary[language]?.[trimmed] || dictionary.ru[trimmed] || trimmed;
    node.nodeValue = source.replace(trimmed, translated);
  }

  function applyLanguage(language) {
    document.documentElement.lang = language;
    document.title = dictionary[language]?.["ogarniemy.pro | мы все уладим"] || dictionary.ru["ogarniemy.pro | мы все уладим"];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        if (node.parentElement.closest("script, style, select")) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    while (walker.nextNode()) translateTextNode(walker.currentNode, language);
  }

  const saved = localStorage.getItem("presentationLanguage") || "pl";
  languageSelect.value = saved;
  applyLanguage(saved);
  languageSelect.addEventListener("change", (event) => {
    localStorage.setItem("presentationLanguage", event.target.value);
    applyLanguage(event.target.value);
  });
})();
