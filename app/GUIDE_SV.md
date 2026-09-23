# Blocket Deal Finder – kom igång

Programmet bevakar Blocket (och Tradera-auktioner samt Facebook Marketplace om du vill), lär sig vad
saker **faktiskt säljs för**, och skickar en push till din telefon när en ny annons ligger långt under det.
Ingenting köps eller skrivs automatiskt. Du får ett försprång, sedan är det du som handlar.

## Starta
Dubbelklicka på `BlocketDealFinder.exe`. Windows kan visa "Windows skyddade datorn" första gången eftersom
programmet inte är signerat av en stor leverantör. Klicka **Mer information** och sedan **Kör ändå**.

Programmet öppnas i ett eget fönster och guidar dig genom tre steg:

1. **Telefonen.** Installera appen ntfy (gratis), tryck på plus i den och skriv in den privata kanal som visas
   i programmet. Tryck "Skicka testnotis" så ser du att det fungerar.
2. **Vad du vill hitta.** Slå på eller av de färdiga bevakningarna och välj region.
3. **Klart.** Tryck "Starta bevakning".

Första omgången lär programmet bara in prisnivåerna, så du dränks inte i gamla annonser. Fynden kommer från
nästa koll, både till telefonen och till fliken **Fynd**.

## Bra att veta
- **Fönstret kan stängas.** Bevakningen fortsätter i bakgrunden. Ikonen nere vid klockan öppnar fönstret igen,
  och där finns "Avsluta" om du vill stänga helt.
- **Starta med Windows** finns under Inställningar. Slå på det och stäng av viloläge, så missar du inget.
- **Egna bevakningar.** Under Bevakningar, "Lägg till egen". Söktexten är det du skulle skriva i Blockets sökruta.
  "Måste innehålla" gör att bara annonser med något av orden räknas, "Uteslut ord" filtrerar bort tillbehör och
  "sökes". "Larma alltid under" är ett fast pris som alltid ger larm.
- **Riktiga försäljningspriser (rekommenderas).** Utropspriser på Blocket ligger ofta 40–50 % över vad saker
  säljs för. Under Inställningar, Avancerat, kan du lägga in en gratis utvecklarnyckel från Tradera
  (api.tradera.com/register). Då lär sig programmet de riktiga priserna från avslutade auktioner och varnar
  dessutom för auktioner som slutar inom två timmar långt under det.
- **Facebook Marketplace (valfritt).** Facebook har inget öppet API. Med ett konto på apify.com (cirka 6 öre per
  annons, 5 dollar gratis per månad) kan programmet hämta Marketplace-annonser några gånger om dagen. Lägg in
  token under Avancerat och en Marketplace-sök-URL på de bevakningar du vill ha den för.

## Läs fynden rätt
Ett fynd visar pris, vad varan vanligen säljs för och ungefär vad du behåller efter försäljningskostnader.
Referensen blandar alla annonser i bevakningen som klarar filtren, så kolla att det är rätt modell och skick.
Var snabb, bra fynd försvinner på minuter. Cyklar: kontrollera alltid ramnumret mot polisens register.

## Licens
Programmet fungerar fullt ut i 14 dagar. Efter köp får du en nyckel kopplad till din e-postadress. Skriv in
båda under Inställningar, Licens, och tryck Aktivera. En licens gäller för en person, hur många datorer som helst.

## Om något strular
Under Inställningar finns "Teknisk logg". Skicka raderna där till supporten så löser vi det. Dina uppgifter
lagras bara på din dator i `%APPDATA%\BlocketDealFinder`.
