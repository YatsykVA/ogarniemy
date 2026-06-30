from extractor import FacebookExtractor

ex = object.__new__(FacebookExtractor)

def clean(text, author=''):
    return ex._clean_post_text(text, author=author)

# 1) Комментарии и кнопки отрезаются, тело объявления остаётся.
out = clean('''Praca skanowanie , pon-pt 6-14/14-22
35.60zł/h brutto
Umowa zlecenie
Od stycznia 2026
Смотреть другие комментарии
Bernardetta Pawlak
Priv
Нравится
Ответить
Поделиться
RadiantAvocado2937''')
assert 'Praca skanowanie' in out and 'Od stycznia 2026' in out
for bad in ['Смотреть', 'Bernardetta', 'Priv', 'Нравится', 'Ответить', 'Radiant']:
    assert bad not in out, (bad, out)

# 2) Дата Facebook выкидывается, объявление остаётся.
out = clean('''17 июнь в 11:01

Praca weekendowa Kamienna Góra

KONTROLA JAKOŚCI Zainteresowanych zapraszamy do kontaktu

696 023 420''')
assert '17 июнь' not in out and 'Praca weekendowa' in out

# 3) Артефакт "и 9" отдельной строкой удаляется, реальные i7/i9 сохраняются.
out = clean('''Intel i9-14900K albo BMW i7 zostaje
и 9''')
assert 'Intel i9-14900K' in out and 'BMW i7' in out and 'и 9' not in out

# 4) Нельзя отрезать нормальный вопрос перед UI-хвостом.
out = clean('''Nie każdy dobry pomysł na biznes potrzebuje wielkiego miasta.
Barber?
Salon beauty lub Head Spa?
Sklep z herbatami i kawami?
Нравится
Ответить
Поделиться''')
assert 'Barber?' in out
assert 'Salon beauty lub Head Spa?' in out
assert 'Sklep z herbatami i kawami?' in out
assert 'Нравится' not in out and 'Ответить' not in out

# 5) Если перед кнопками есть имя автора комментария — удаляется комментарий целиком.
out = clean('''Poszukuję mam i kobiet do pracy online!
Jeśli możesz poświęcić 2–3 godziny dziennie.
Oferujemy:
pracę z domu,
szkolenie od podstaw,
Ewa Kapustka
Proszę o info - zainteresowana - jaka to praca ?
Нравится
Ответить
Поделиться
Показать 1 ответ
Roksana Rzepa
Priv''')
assert 'Poszukuję mam' in out and 'szkolenie od podstaw' in out
for bad in ['Ewa Kapustka', 'Proszę o info', 'Нравится', 'Roksana', 'Priv']:
    assert bad not in out, (bad, out)

# 6) Декоративные маркеры удаляются.
out = clean('''Ten zegarek przetrwa polskie upały
•
•
•
Dostępny w Loombardzie za jedyne 699 zł!
ul. Długa 45B Świdnica''')
assert '•' not in out and 'Dostępny' in out

# 7) Явно нераскрытый хвост не отправляем.
assert ex._is_good_post_text('Łączę b...') is False
assert ex._is_good_post_text('Normalny pełny tekst') is True

print('OK: v31 extractor tests passed')
