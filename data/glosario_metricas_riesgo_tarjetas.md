# Diccionario y Glosario Oficial de Métricas de Riesgo, Cosechas (Vintages) y Cartera de Tarjetas de Crédito Banamex

Este documento establece la terminología oficial, fórmulas financieras, reglas contables y dimensiones analíticas para la gestión de riesgo y portafolio de tarjetas de crédito en Grupo Financiero Banamex.

---

## 1. Dimensiones de Origen, Adquisición y Segmentación

### VINTAGE (Cosecha de Originación)
* **Definición:** Identifica el mes y año de apertura y alta contable (*Booking*) de la cuenta de tarjeta de crédito.
* **Formato Estándar:** `AAAAMM` (Ejemplo: `202401` para Enero de 2024, `202609` para Septiembre de 2026).
* **Rango Histórico de Información:** Desde `202401` hasta `202609`.

### STRATEGY (Estrategia de Adquisición)
* **Definición:** Modelo y política crediticia bajo la cual se originó y aprobó la cuenta en función de su fecha de originación (*VINTAGE*).
* **Valores y Reglas de Negocio:**
  * **14.Recalibration 1.0:** Cuentas originadas entre `202401` y `202405`.
  * **15.Recalibration 2.0:** Cuentas originadas entre `202406` y `202507`.
  * **r.Reca champ:** Cuentas originadas entre `202508` y `202510`.
  * **Cosechas 202511 a 202607:** Asignación combinada: 70% bajo estrategia `r.Reca champ` y 30% bajo modelo predictivo `DARIA 1.6`.
  * **Cosechas 202608 a 202609:** Asignación combinada: 50% bajo estrategia `r.Reca champ` y 50% bajo modelo predictivo `DARIA 2.0`.

### STRATEGY_SEGMENT (Segmento de Estrategia)
* **Definición:** Sub-segmento operativo y comercial asignado a la cuenta según la estrategia de originación.
* **Valores bajo Estrategias Recalibration / Reca champ:**
  * `Brands-NonPayroll-OffUs&LE`
  * `Brands-NonPayroll-OnUs`
  * `Brands-Payroll-OffUs&LE`
  * `Brands-Payroll-OnUs`
  * `Costco`
  * `HomeDepot & Affinity`
  * `Priority & Affluent`
* **Valores bajo Estrategias DARIA (1.6 / 2.0):**
  * `AAC Off Us Non-Payroll`, `AAC Off Us Payroll`, `AAC On Us`, `Affinity`, `Affluent`, `Costco - Young <31`, `Costco - Non-LE`, `Costco - LE`, `Home Depot - Young <31`, `Home Depot - LE`, `Home Depot - Non-LE`, `LE Commun. & NTR`, `LE Inq Only & No Hit`, `LE Thin`, `Priority Qualified`, `Young < 25`, `Young 25-30`.

### PRODUCT (Catálogo de Productos de Tarjetas)
* **Definición:** Tipo de tarjeta de crédito emitida al cliente en la apertura.
* **Catálogo Oficial (12 Productos):**
  1. `Affinity` (Affinity Card Citibanamex)
  2. `Clasica` (Tarjeta Clásica Banamex)
  3. `Conquista` (Tarjeta Conquista)
  4. `Costco` (Tarjeta Costco Citibanamex)
  5. `Descubre` (Tarjeta Descubre)
  6. `Explora` (Tarjeta Explora)
  7. `Home Depot` (Tarjeta The Home Depot Citibanamex)
  8. `Joy` (Tarjeta Joy Banamex)
  9. `Oro` (Tarjeta Oro Citibanamex)
  10. `Platinum` (Tarjeta Platinum Citibanamex)
  11. `Teleton` (Tarjeta Teletón)
  12. `Beyond` (Tarjeta Beyond / Citibanamex Prestige)

### CHANNEL (Canal de Originación / Booking)
* **Definición:** Canal comercial o medio a través del cual el cliente solicitó y contrató la tarjeta de crédito.
* **Valores Oficiales:** `Aeropuerto`, `Affinity`, `Beyond/Prestige`, `Branch` (Sucursal tradicional), `Costco`, `F2F` (Face to Face / Módulos de venta presencial), `Home Depot`, `Internet` (Banca digital web), `One Click` (Ofertas preaprobadas en App móvil), `Others`, `TMK` (Telemarketing / Ventas telefónicas), `Turismo`.

### AGE_TAG (Grupo Etario de Origen)
* **Definición:** Segmentación por rango de edad del titular al momento de la aprobación de la cuenta.
* **Rangos Estándar:**
  * `01| Young < 25`: Clientes menores de 25 años.
  * `02| Young 25-30`: Clientes de 25 a 30 años.
  * `03| Mature >30`: Clientes mayores de 30 años.

### SCORE_TAG (Puntaje de Riesgo de Adquisición)
* **Definición:** Escala de calificación crediticia asignada al cliente al momento de evaluar su solicitud.
* **Rangos bajo Estrategias Recalibration / Reca champ:** `<130`, `130-140`, `141-150`, `151-160`, `161-170`, `171-180`, `181-190`, `190+`.
* **Rangos bajo Estrategias DARIA:** `a.<=154`, `b.155-159`, `c.160-164`, `d.165-169`, `e.170-174`, `f.175-189`, `g.190-199`, `h.200-204`, `i.205+`.

---

## 2. Maduración y Temporalidad

### MOB (Month On Book / Meses de Maduración)
* **Definición:** Métrica fundamental de cosechas que indica la cantidad de meses completos transcurridos desde la fecha de apertura (`VINTAGE`) de la cuenta.
* **Reglas y Rango:**
  * `MOB = 0`: Mes calendario en el que la cuenta fue abierta e incorporada al balance contable.
  * `MOB = 1`: Primer mes posterior al mes de apertura.
  * **Rango:** Enteros continuos de `0` a `32` meses.
  * Cuentas con `VINTAGE = 202401` alcanzan hasta `MOB = 32`.
  * Cuentas recientes (ej. `VINTAGE = 202609`) únicamente tienen información en `MOB = 0`.

---

## 3. Volumetría y Balances de Cartera

### ACCOUNTS (Cantidad de Cuentas)
* **Definición:** Número total de cuentas de crédito que comparten exactamente las mismas características de originación (`VINTAGE`, `STRATEGY`, `STRATEGY_SEGMENT`, `PRODUCT`, `CHANNEL`, `AGE_TAG`, `SCORE_TAG`) en el mes de performance evaluado (`MOB`).

### CREDITLINESAMTS (Líneas de Crédito Totales)
* **Definición:** Monto acumulado de las líneas de crédito límite autorizadas en pesos mexicanos para el grupo de cuentas analizado.

### ENRACCTS (Cuentas con Saldo Deudor / Revolvente)
* **Definición:** Cantidad de cuentas del grupo que mantienen saldos deudores activos (*Ending Net Receivables Accounts*) al cierre del periodo.

### ENRAMTS (Saldo Deudor Total / ENR)
* **Definición:** Suma total en pesos mexicanos de los saldos deudores que mantienen las cuentas activas al corte (*Ending Net Receivables Amounts*).

---

## 4. Morosidad y Deterioro de Cartera

### OVER30ACCTS (Cuentas en Mora Mayor a 30 Días)
* **Definición:** Cantidad de cuentas que registran un atraso de pago superior a 30 días naturales a la fecha de corte (*Over 30 Days Delinquent Accounts*). Representa el primer indicador formal de deterioro crediticio de la cosecha.

### OVER30AMTS (Saldo Vencido en Mora Mayor a 30 Días)
* **Definición:** Suma total de saldos deudores asociados a las cuentas que cayeron en atraso mayor a 30 días (*Over 30 Delinquency Amounts*).

---

## 5. Pérdidas Crediticias, Castigos y Recuperaciones

### GCLAMTS (Gross Credit Loss / Pérdida Bruta de Crédito)
* **Definición:** Saldo acumulado en pesos de las cuentas que alcanzaron la condición de incobrabilidad o castigo contable (*Charge-off / Gross Credit Loss*).

### RECOVERYAMTS (Recuperación de Cartera Castigada)
* **Definición:** Montos en pesos recuperados por Banamex derivados de cobranza especializada, reestructuras o liquidación de cuentas que habían sido previamente castigadas en `GCLAMTS`.

### NCLAMTS (Net Credit Loss / Pérdida Neta Crediticia)
* **Definición:** Pérdida crediticia real neta absorbida por el banco tras deducir las recuperaciones logradas.
* **FÓRMULA CONTABLE OFICIAL OBLIGATORIA:**
  $$\text{NCLAMTS} = \text{GCLAMTS} - \text{RECOVERYAMTS}$$
* **Interpretación:** La Pérdida Neta (NCL) siempre equivale a la Pérdida Bruta (GCL) restando la cobranza recuperada (RECOVERY).

---

## 6. Estado de Resultados Financiero (P&L: Ingresos y Gastos)

### REVENUE (Ingresos Totales de Cartera)
* **Definición:** Suma de ingresos financieros generados por el grupo de cuentas (intereses ordinarios, intereses moratorios, comisiones por anualidad, comisiones por disposición de efectivo y penalizaciones).

### EXPENSES_ACQ (Gastos de Adquisición de Clientes)
* **Definición:** Gastos comerciales, de marketing, comisiones de venta y costo de originación incurridos para captar y emitir las tarjetas.
* **REGLA CONTABLE ESTRICTA:** Los gastos de adquisición se reconocen contablemente **exclusivamente en el mes de originación (`MOB = 0`)**. Para cualquier mes de maduración posterior (`MOB > 0`), el valor de `EXPENSES_ACQ` es estrictamente `$0.00`.

### EXPENSES_ECM_VAR (Gastos Variables de Gestión de Cartera)
* **Definición:** Gastos operativos variables asociados al mantenimiento del ciclo de vida de las cuentas (*Existing Customer Management Variable Expenses*), tales como procesamiento transaccional, estados de cuenta y atención telefónica variable.

### EXPENSES_ECM_FIX (Gastos Fijos de Gestión de Cartera)
* **Definición:** Costos fijos indirectos y operativos de administración, tecnología y plataforma asignados a la cartera (*Existing Customer Management Fixed Expenses*).

### EXPENSES_TOTAL (Gastos Totales de Portafolio)
* **Definición:** Gasto financiero integral acumulado para el mantenimiento y administración del conjunto de cuentas.
* **FÓRMULA CONTABLE OFICIAL OBLIGATORIA:**
  $$\text{EXPENSES\_TOTAL} = \text{EXPENSES\_ACQ} + \text{EXPENSES\_ECM\_VAR} + \text{EXPENSES\_ECM\_FIX}$$
* **Interpretación:** El gasto total es la suma de los costos de adquisición de clientes más los gastos variables y fijos de gestión de cartera.
