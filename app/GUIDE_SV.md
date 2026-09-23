# Blocket Deal Finder – kom igång på 5 minuter

Programmet bevakar Blocket (och Tradera-auktioner samt Facebook Marketplace om du vill), lär sig vad
saker **faktiskt säljs för**, och skickar en push till din telefon när en ny annons ligger långt under det.
Ingenting köps eller skrivs automatiskt. Du får ett försprång, sedan är det du som handlar.

## 1. Starta programmet
Dubbelklicka på `BlocketDealFinder.exe`. Windows kan visa "Windows skyddade datorn" eftersom programmet
inte är signerat av en stor leverantör. Klicka **Mer information** och sedan **Kör ändå**. Det händer bara
första gången.

## 2. Koppla telefonen (2 minuter)
1. Installera appen **ntfy** på telefonen (App Store / Google Play, gratis).
2. Tryck på plus och prenumerera på ett hemligt ämnesnamn, till exempel `fynd-anna-8k2m`. Välj något
   ingen kan gissa.
3. Skriv samma namn i programmet under **Inställningar > ntfy-ämne** och tryck **Skicka testnotis**.

## 3. Välj vad du vill bevaka
Fliken **Bevakningar** har färdiga bevakningar för saker som brukar gå att flippa: verktyg (Festool, Hilti,
Makita), barnvagnar (Bugaboo, Thule), elcyklar, designlampor, grillar, robotgräsklippare, kameraobjektiv.
Ta bort det du inte bryr dig om, lägg till egna. Tips för egna bevakningar:
- **Söktext** är det du skulle skriva i Blockets sökruta.
- **Måste innehålla** gör att bara annonser med något av orden räknas (t.ex. modellnamn).
- **Uteslut ord** filtrerar bort tillbehör, reservdelar och "sökes"-annonser.
- **Larma alltid under** är ett fast pris som alltid ger larm, bra för märken med många modeller.

Tryck **Starta bevakning**. Första passet lär bara in prisnivåerna, så du dränks inte i gamla annonser.
Larmen kommer från nästa pass.

## 4. Riktiga försäljningspriser (rekommenderas)
Utropspriser på Blocket ligger ofta 40–50 % över vad saker faktiskt säljs för. Med en gratis
utvecklarnyckel från Tradera lär sig programmet de riktiga priserna från avslutade auktioner och varnar
dessutom för auktioner som slutar inom två timmar långt under det.
1. Gå till https://api.tradera.com/register och skapa en app (gratis).
2. Klistra in App ID och App Key under **Inställningar**.

Utan nyckel använder programmet Blockets utropspriser nedskalade till typiskt försäljningsvärde.

## 5. Facebook Marketplace (valfritt, kostar lite)
Facebook har inget öppet API. Programmet kan använda tjänsten Apify (apify.com) som hämtar annonser åt
dig; det kostar cirka 6 öre per annons och du får 5 dollar gratis varje månad. Skapa konto, kopiera
API-token (Settings > Integrations) till **Inställningar > Apify-token**, och klistra in en
Marketplace-sök-URL (kopierad från webbläsaren, med ditt område och "Datum publicerad" som sortering) på
de bevakningar du vill ha den för.

## 6. Läs larmen rätt
Ett larm säger till exempel: *"Bugaboo Fox 3 ... 1 200 kr. 'Bugaboo' SÄLJS vanligen för ca 2 600 kr
(grund: riktiga försäljningspriser på Tradera, 10 affärer), så detta är 54 % under. Efter ca 12 %
försäljningskostnader behåller du ca 2 290 kr = ca 1 090 kr marginal."*
- Referensen blandar alla annonser som klarar dina filter. Kolla att det är rätt modell och skick.
- Var snabb. Bra fynd försvinner på minuter.
- Cyklar: kontrollera alltid ramnumret mot polisens register innan du betalar.
- Fliken **Fynd** visar alla larm hittills med länkar.

## 7. Licens
Programmet fungerar fullt ut i 14 dagar. Efter köp får du en nyckel kopplad till din e-postadress; skriv
in båda under **Inställningar > Licens** och tryck **Aktivera**. En licens gäller för en person, hur många
datorer som helst.

## Vanliga frågor
**Måste programmet vara igång?** Ja. Bevakningen körs på din dator. Kryssa i **Starta med Windows** och låt
datorn vara på, eller stäng av viloläge.
**Var lagras mina uppgifter?** Bara på din dator, i `%APPDATA%\BlocketDealFinder`. Ingenting skickas
någonstans utom pushnotiserna till ditt eget ntfy-ämne.
**Kan programmet köpa åt mig?** Nej, och det är avsiktligt. Blocket tillåter det inte och det skulle kosta dig
pengar på misstag.
**Blocket ändrade något och det slutade fungera?** Hör av dig, så uppdaterar jag programmet.
