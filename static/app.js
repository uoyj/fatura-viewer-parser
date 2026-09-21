"use strict";

// ============================================================================
// fatura-viewer — front migrado para Alpine.js 3 (vendor local em
// /vendor/alpine.min.js: sem CDN, sem build, sem node_modules).
//
// O que mudou de mecânica (a lógica é a MESMA de antes):
//   * estado global espalhado em variáveis  → um componente Alpine (x-data="app")
//   * innerHTML montado na mão             → <template x-for> + x-text/x-html
//   * style.display none/block             → x-show / x-if
//   * addEventListener / onclick inline    → @click / @submit.prevent / @keydown
//   * fetch(...).then(render manual)       → métodos do componente que só
//     atualizam o estado; o Alpine re-renderiza sozinho
//
// O que NÃO migra de propósito: o desenho dos gráficos (SVG de linha da projeção
// e pizza em conic-gradient) continua em funções utilitárias fora do componente,
// chamadas pelo Alpine via x-html.
// ============================================================================

// ===== Placeholders / rótulos fixos =====
// Regra do FRONT 3: NADA é auto-selecionado. faturas[0] nunca vira seleção
// implícita — o header mostra o placeholder até o usuário clicar numa opção
// (clique explícito) ou até um upload bem-sucedido definir a fatura atual.
const PLACEHOLDER_FATURA = "Selecione uma fatura...";
const PLACEHOLDER_ARQUIVO = "📎 Clique para selecionar PDF";

// ===== Colunas da tabela de transações =====
const COLUNAS = [
  {key:"data", label:"Data"},
  {key:"descricao", label:"Descrição"},
  {key:"cartao", label:"Cartão"},
  {key:"parcela", label:"Parcela"},
  {key:"categoria", label:"Categoria"},
  {key:"valor", label:"Valor"},
];

// ===== Utils =====
function formatarMoeda(v) {
  if (v === null || v === undefined || v === "") return "—";
  const n = parseFloat(v);
  return new Intl.NumberFormat('pt-BR', {style:'currency', currency:'BRL'}).format(n);
}

function formatarData(iso) {
  if (!iso) return "—";
  const [y, m, d] = iso.split('-');
  return `${d}/${m}/${y}`;
}

function formatarParcela(parcela_atual, parcela_total) {
  if (!parcela_atual || !parcela_total) return "—";
  return `${parcela_atual}/${parcela_total}`;
}

// Rótulo de banco: primeira letra maiúscula (mesmo texto que ia para as options)
function rotuloBanco(b) {
  return b.charAt(0).toUpperCase() + b.slice(1);
}

// "2026-09" -> "09/2026"
function rotuloMes(m) {
  return String(m).split("-").reverse().join("/");
}

// Textarea de regra -> array de padrões (uma linha por padrão, vazias fora)
function padroesDe(texto) {
  return String(texto || "").split("\n").map(p => p.trim()).filter(p => p !== "");
}

// Resumo de uma fatura no dropdown/palette: "banco · fechamento · total"
function formatoResumoFatura(f) {
  const total = formatarMoeda(f.total_a_pagar);
  const data = f.fechamento || "?";
  return `${f.banco} · ${data} · ${total}`;
}

// ===== Cores de categoria: paleta qualitativa + atribuição greedy persistente =====
// Hash puro colidia (hsl(hash%360, ...)): cores repetidas com ~15 categorias.
// Aqui cada categoria NOVA pega a próxima cor LIVRE da paleta curada — duas
// categorias nunca dividem cor até a paleta esgotar (aí recicla com outra
// luminância). Persistido em localStorage: estável entre telas e sessões.
const PALETA_CATEGORIAS = [
  "hsl(210, 75%, 55%)",   // azul
  "hsl(25, 85%, 55%)",    // laranja
  "hsl(150, 60%, 45%)",   // verde
  "hsl(330, 70%, 58%)",   // rosa
  "hsl(270, 65%, 62%)",   // roxo
  "hsl(45, 90%, 50%)",    // amarelo
  "hsl(180, 70%, 40%)",   // teal
  "hsl(0, 75%, 58%)",     // vermelho
  "hsl(95, 55%, 45%)",    // verde-limão
  "hsl(305, 60%, 58%)",   // magenta
  "hsl(160, 45%, 62%)",   // menta claro
  "hsl(230, 45%, 65%)",   // azul-acinzentado
];
const CORES_CATEGORIA = new Map(
  JSON.parse(localStorage.getItem("coresCategorias") || "[]"));

function _salvarCoresCategoria() {
  try {
    localStorage.setItem("coresCategorias", JSON.stringify([...CORES_CATEGORIA]));
  } catch (_) { /* armazenamento indisponível: cores apenas na sessão */ }
}

function corCategoria(nome) {
  if (!nome) return "rgba(148,163,184,0.3)";
  if (!CORES_CATEGORIA.has(nome)) {
    const usadas = new Set(CORES_CATEGORIA.values());
    let cor = PALETA_CATEGORIAS.find(c => !usadas.has(c));
    if (!cor) {
      // Paleta esgotada: recicla variando a luminância a cada volta
      const i = CORES_CATEGORIA.size % PALETA_CATEGORIAS.length;
      const m = PALETA_CATEGORIAS[i].match(/^hsl\((\d+),\s*(\d+)%,\s*(\d+)%\)$/);
      const voltas = Math.floor(CORES_CATEGORIA.size / PALETA_CATEGORIAS.length);
      const lum = Math.min(35 + voltas * 15, 70);
      cor = `hsl(${m[1]}, ${m[2]}%, ${lum}%)`;
    }
    CORES_CATEGORIA.set(nome, cor);
    _salvarCoresCategoria();
  }
  return CORES_CATEGORIA.get(nome);
}

// Borda da célula de categoria: só a BORDA usa a cor da categoria (hsl() válido).
// Fundo/cor do texto vêm do CSS (tema escuro) — append de alpha em hsl() é
// inválido e pintava branco.
function corBordaCategoria(categoria) {
  return categoria ? corCategoria(categoria) : "rgba(148,163,184,0.4)";
}

function escHtml(s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// MESMO algoritmo de categorizer.normalizar_descricao (backend):
// upper + colapso de espaços múltiplos em 1 + strip
function normalizarDescricao(s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/\s+/g, " ")
    .trim()
    .toUpperCase();
}

// ===== API calls (inalterado) =====
async function apiGET(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

async function apiDELETE(url) {
  const resp = await fetch(url, { method: 'DELETE' });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

async function apiFetch(url, opts) {
  const resp = await fetch(url, opts);
  if (!resp.ok) {
    // Erros da API vêm como {"erro": "..."}; 404/HTTPException como {"detail": "..."}
    let msg = `HTTP ${resp.status}`;
    try {
      const j = await resp.json();
      msg = j.erro || j.detail || msg;
    } catch (_) { /* corpo não-JSON */ }
    throw new Error(msg);
  }
  return resp.json();
}

async function apiPUT(url, body) {
  return apiFetch(url, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

async function apiPOST(url, body) {
  const opts = { method: 'POST' };
  if (body !== undefined) {
    opts.headers = { 'Content-Type': 'application/json' };
    opts.body = JSON.stringify(body);
  }
  return apiFetch(url, opts);
}

// ===== Helpers de render (fora do componente, usados via x-text/x-html) =====

// Mapa categoria -> valor (débitos) -> itens ordenados com a largura relativa.
// As barras usam pct relativo ao MAIOR valor; a pizza usa o par [rótulo, valor].
function itensCategoria(porCat) {
  const itens = Object.entries(porCat || {}).sort((a, b) => b[1] - a[1]);
  const max = itens[0]?.[1] || 0;
  return itens.map(([cat, valor]) => ({
    cat,
    valor,
    pct: max > 0 ? Math.round((valor / max) * 100) : 0,
  }));
}

function paresCategoria(porCat) {
  return Object.entries(porCat || {}).sort((a, b) => b[1] - a[1]);
}

// --- Regras em edição: cada bloco ganha um uid estável só para o :key do x-for
let regraSeq = 0;
function novaRegra(categoria, padroes) {
  return {
    _uid: ++regraSeq,
    categoria: categoria || "",
    _texto: (padroes || []).join("\n"),
    _invalido: false,
  };
}

// ===== Pizza (conic-gradient + legenda, sem biblioteca) =====
// itens: [[rotulo, valorNum], ...]
function htmlPizza(itens) {
  const total = itens.reduce((acc, [, v]) => acc + v, 0);
  if (!total) return '<span style="color:var(--muted);font-size:0.85rem;">Sem débitos no período.</span>';
  const top = itens.slice(0, 8);
  const resto = itens.slice(8).reduce((acc, [, v]) => acc + v, 0);
  if (resto > 0) top.push(["Outros", resto]);

  let acc = 0;
  const segs = top.map(([rotulo, v]) => {
    const pct = (v / total) * 100;
    const seg = `${corCategoria(rotulo)} ${acc}% ${acc + pct}%`;
    acc += pct;
    return seg;
  }).join(",");

  const legenda = top.map(([rotulo, v]) =>
    `<div class="pl-linha"><span class="pizza-cor" style="background:${corCategoria(rotulo)}"></span>` +
    `<span>${escHtml(rotulo)}</span><span class="pl-valor">${formatarMoeda(v)} · ${((v / total) * 100).toFixed(0)}%</span></div>`
  ).join("");

  return `<div class="pizza-wrap"><div class="pizza" style="background:conic-gradient(${segs})"></div>` +
         `<div class="pizza-legenda">${legenda}</div></div>`;
}

// ===== Gráfico de linha da projeção (SVG vanilla, sem biblioteca) =====
let linhaSeq = 0;

// pontos: [{rotulo: "10/2026", valor: 180}] — ordenados por mês
function htmlLinhaProjecao(pontos) {
  if (!pontos.length) {
    return '<span style="color:var(--muted);font-size:0.85rem;">Sem dados para projeção.</span>';
  }
  const W = 640, H = 200, L = 46, R = 10, T = 16, B = 24;
  const iw = W - L - R, ih = H - T - B;
  const max = Math.max(...pontos.map(p => p.valor));
  const escala = max > 0 ? max : 1;
  const xy = pontos.map((p, i) => ({
    x: L + (pontos.length === 1 ? iw / 2 : (i / (pontos.length - 1)) * iw),
    y: T + ih - (p.valor / escala) * ih,
  }));
  const path = xy.map((p, i) => `${i ? "L" : "M"}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
  const area = `${path} L${xy[xy.length - 1].x.toFixed(1)},${T + ih} L${xy[0].x.toFixed(1)},${T + ih} Z`;
  const gid = "gproj" + (++linhaSeq);   // id único por render (gradiente)
  const dots = xy.map((p, i) =>
    `<circle class="linha-ponto" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="4"
       fill="var(--accent)" stroke="var(--bg)" stroke-width="1.5" style="cursor:pointer"
       data-rotulo="${escHtml(pontos[i].rotulo)}" data-valor="${pontos[i].valor}"></circle>`
  ).join("");
  // Rótulos de mês "10/2026" → "10/26"; com muitos pontos, pula alternados
  const labels = pontos.map((p, i) => {
    if (pontos.length > 8 && i % 2 === 1) return "";
    const partes = String(p.rotulo).split("/");
    const curto = partes.length === 2 ? `${partes[0]}/${partes[1].slice(2)}` : p.rotulo;
    return `<text x="${xy[i].x.toFixed(1)}" y="${H - 6}" font-size="9" fill="var(--muted)" text-anchor="middle">${escHtml(curto)}</text>`;
  }).join("");
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block" role="img" aria-label="Projeção de gastos por mês">
    <defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.30"/>
      <stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/>
    </linearGradient></defs>
    <line x1="${L}" y1="${T}" x2="${L}" y2="${T + ih}" stroke="var(--border)"/>
    <line x1="${L}" y1="${T + ih}" x2="${L + iw}" y2="${T + ih}" stroke="var(--border)"/>
    <path d="${area}" fill="url(#${gid})"/>
    <path d="${path}" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    ${dots}
    <text x="${L}" y="${T - 4}" font-size="10" fill="var(--muted)" text-anchor="start">${formatarMoeda(max)}</text>
    ${labels}
  </svg>`;
}

// ===== Tooltips / textos fixos do ensino =====
const TOOLTIP_ENSINAR = "Ensinar categoria — vale para todas as faturas";
const TOOLTIP_RECORRENTE = "Marcar como gasto recorrente — projeta este valor nos próximos 12 meses (vale para todas as faturas)";
const TOOLTIP_RECORRENTE_ATIVO = "Gasto recorrente — projetado indefinidamente. Clique para desmarcar.";
const MSG_CONVERTER_REGRA = "Converter ensino em regra? A regra passa a casar com qualquer descrição que contenha o padrão.";

// Sentinela do select inline: abre o editor livre e NUNCA vai ao backend.
const SENTINELA_OUTRA = "__OUTRA__";

// Ícone SVG inline do badge recorrente (substitui o emoji 🔁 para consistência
// visual entre a pílula do toggle e o badge da projeção — tupla de geometria
// (.badge/.marca-recorrente) preservada via CSS abaixo).
// Padrão: Lucide "repeat" (lucide-static v1.47.0, licença ISC), copiado do pacote
// oficial — é SVG de TRAÇO (fill=none + stroke=currentColor), por isso o CSS de
// .icon-recorrente precisa zerar o fill herdado da regra antiga.
// Alternativas oficiais do mesmo pacote, se preferir trocar em uma linha:
// "rotate-cw", "refresh-cw", "refresh-ccw", "calendar-sync", "recycle", "infinity".
const ICONE_RECORRENTE = '<svg class="icon-recorrente" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m17 2 4 4-4 4"/><path d="M3 11v-1a4 4 0 0 1 4-4h14"/><path d="m7 22-4-4 4-4"/><path d="M21 13v1a4 4 0 0 1-4 4H3"/></svg>';

// ============================================================================
// Componente Alpine único (x-data="app" no <body>)
// ============================================================================
document.addEventListener("alpine:init", () => {
  Alpine.data("app", () => ({
    // ===== Telas / navegação =====
    tela: "upload",              // upload | viewer | dicionario | comparativo | consolidado
    view: { name: "upload", id: null },   // view derivada da URL ({ name, id }) — history routing
    carregando: false,           // true enquanto route() carrega os dados da view da URL
    rotaToken: 0,                // descarta resposta de navegação já obsoleta (back/forward rápido)
    secaoAtiva: null,            // scroll-spy da anchor-nav
    scrollIntencional: false,    // ignora o scroll-spy durante o scroll programático

    // ===== Faturas =====
    faturas: [],                 // lista do GET /faturas
    fatura: null,                // fatura carregada (objeto completo)
    linhas: [],                  // transações após filtro (ordenação é derivada)
    busca: "",
    filtroCategoria: "",
    filtroCartao: "",
    sort: { key: "data", dir: "asc" },
    selectsPopulados: false,     // controle: popular selects só uma vez por fatura
    categoriasFiltro: [],        // snapshot por fatura (fora de escopo do autocomplete)
    cartoesFiltro: [],

    // ===== Recorrentes / categorias conhecidas =====
    recorrentes: new Set(),        // descrições normalizadas marcadas como recorrentes
    categoriasConhecidas: new Set(["Outros"]),  // rótulos conhecidos — CACHE p/ autocomplete/dedup
    aprendizadoFlash: null,        // chave destacada após um ensino (flash ~800ms)
    emCursoCat: new Set(),         // ids com PUT de categoria em voo
    emCursoRec: new Set(),         // ids com PUT/DELETE de recorrente em voo
    editor: null,                  // { id, valor } — editor livre de categoria (um por vez)

    // ===== Upload =====
    parsers: {},
    banco: "",
    arquivo: null,
    arquivoNome: PLACEHOLDER_ARQUIVO,
    bancoHint: { texto: "", tom: "" },
    enviando: false,
    uploadErro: "",

    // ===== Command palette =====
    cmdkAberto: false,
    cmdkBusca: "",
    cmdkAtivo: -1,

    // ===== Dicionário de categorias =====
    regras: [],                    // regras em edição (espelho de data/categorias.json)
    overrides: {},                 // ensinamentos manuais (GET /overrides)
    overridesErro: "",
    msgRegras: { texto: "", tipo: "" },
    msgOverrides: { texto: "", tipo: "" },
    msgRecat: { texto: "", tipo: "" },
    recategorizando: false,

    // ===== Comparativo / Consolidado =====
    comp: null,
    compErro: "",
    compVazio: false,
    cons: null,
    consErro: "",
    consVazio: false,
    consMes: "",

    // ===== Vistas (toggles) — reiniciam a cada re-render, como o
    // dataset.vista + style.display do código anterior =====
    vistaCat: "pizza",
    vistaConsCat: "pizza",
    vistaProj: "grafico",
    vistaConsProj: "grafico",

    // ===== Projeção (blocos/pontos ficam em estado p/ o toggle de detalhes) =====
    blocosProj: [],
    pontosProj: [],
    projMensagem: "",
    blocosConsProj: [],
    pontosConsProj: [],
    consProjMensagem: "",

    // ===== Tooltip do gráfico de linha =====
    tooltip: { visivel: false, rotulo: "", valor: "", left: 0, top: 0 },

    _dicTimers: {},   // timers das mensagens que somem sozinhas
    _tAprend: null,

    // ---------------------------------------------------------------- derivados
    // Transações filtradas + ordenadas (a ordenação não muda o filtro: o footer
    // e os totais por categoria usam `linhas`, como antes)
    get linhasOrdenadas() {
      const { key, dir } = this.sort;
      return [...this.linhas].sort((a, b) => {
        let av = a[key], bv = b[key];
        if (key === "valor") { av = parseFloat(av); bv = parseFloat(bv); }
        if (key === "data") { av = av || ""; bv = bv || ""; }
        if (av < bv) return dir === 'asc' ? -1 : 1;
        if (av > bv) return dir === 'asc' ? 1 : -1;
        return 0;
      });
    },

    get somaFiltrada() {
      const soma = this.linhas.reduce((acc, t) => acc + parseFloat(t.valor), 0);
      return formatarMoeda(soma);
    },

    // Total por categoria (débitos) das transações filtradas — vista dual
    get porCategoria() {
      const porCat = {};
      this.linhas.forEach(t => {
        if (parseFloat(t.valor) > 0) {
          const c = t.categoria || "—";
          porCat[c] = (porCat[c] || 0) + parseFloat(t.valor);
        }
      });
      return porCat;
    },
    get barrasCategorias() { return itensCategoria(this.porCategoria); },
    get paresCategorias() { return paresCategoria(this.porCategoria); },

    // Header cards da fatura carregada
    get cardsResumo() {
      const f = this.fatura || {};
      return [
        { label: "Fechamento", value: formatarData(f.fechamento), cls: "" },
        { label: "Vencimento", value: formatarData(f.vencimento), cls: "" },
        { label: "Total a pagar", value: formatarMoeda(f.total_a_pagar), cls: "total" },
        { label: "Pagamento mínimo", value: formatarMoeda(f.pagamento_minimo), cls: "" },
      ];
    },

    get bancosDisponiveis() { return Object.keys(this.parsers); },

    // Categorias oferecidas no select inline: as da fatura carregada + os rótulos
    // CONHECIDOS (outras faturas, regras, overrides) + "Outros", ordenadas.
    // Lê o cache a cada render: categoria criada agora já entra no select das demais
    // linhas no próximo render.
    get categoriasDisponiveis() {
      const cats = new Set(
        ((this.fatura && this.fatura.transacoes) || [])
          .map(t => t.categoria)
          .filter(c => c && c !== "—")
      );
      this.categoriasConhecidas.forEach(c => { if (c && c !== "—") cats.add(c); });
      cats.add("Outros");
      return [...cats].sort((a, b) => a.localeCompare(b, "pt-BR"));
    },

    // datalist COMPARTILHADO (um só no body): options = categoriasConhecidas ordenadas
    get datalistCategorias() {
      return [...this.categoriasConhecidas]
        .sort((a, b) => a.localeCompare(b, "pt-BR"));
    },

    // Itens visíveis no command palette, já na ORDEM do DOM (mês desc, depois a
    // ordem da lista) — é esse índice que o teclado navega.
    get cmdkVisiveis() {
      const q = (this.cmdkBusca || "").trim().toLowerCase();
      const filtradas = this.faturas.filter(f =>
        !q || formatoResumoFatura(f).toLowerCase().includes(q));
      const porMes = {};
      filtradas.forEach(f => {
        const m = (f.fechamento || "?").slice(0, 7);
        (porMes[m] = porMes[m] || []).push(f);
      });
      const out = [];
      Object.keys(porMes).sort().reverse().forEach(mes => {
        porMes[mes].forEach((f, i) => out.push({
          id: f.id,
          resumo: formatoResumoFatura(f),
          mesLabel: mes === "????-??" ? "Sem data" : rotuloMes(mes),
          novoMes: i === 0,
        }));
      });
      return out;
    },

    // Overrides ordenados por descrição (pt-BR)
    get chavesOverrides() {
      return Object.keys(this.overrides).sort((a, b) => a.localeCompare(b, "pt-BR"));
    },

    // Mês atual do consolidado (débitos por categoria) — vista dual
    get consPorCategoria() {
      const c = this.cons;
      if (!c || !this.consMes) return {};
      const porCat = {};
      Object.entries(c.por_categoria || {}).forEach(([cat, pm]) => {
        if (pm[this.consMes] !== undefined) porCat[cat] = parseFloat(pm[this.consMes]);
      });
      return porCat;
    },
    get barrasConsMes() { return itensCategoria(this.consPorCategoria); },
    get paresConsMes() { return paresCategoria(this.consPorCategoria); },
    get mesesCons() { return (this.cons && this.cons.meses) || []; },

    // ------------------------------------------------------- comparativo
    get mesesComp() { return (this.comp && this.comp.meses) || []; },

    // Linha total primeiro, depois categorias ordenadas pelo maior gasto agregado
    get categoriasComp() {
      const c = this.comp;
      if (!c) return [];
      const anomalias = new Set((c.anomalias_categoria || []).map(a => `${a.categoria}|${a.mes}`));
      return Object.entries(c.por_categoria)
        .map(([nome, porMes]) => [nome, c.meses.reduce((acc, m) => acc + (parseFloat(porMes[m]) || 0), 0)])
        .sort((a, b) => b[1] - a[1])
        .map(([nome]) => {
          const porMes = c.por_categoria[nome];
          const anom = {};
          c.meses.forEach(m => { anom[m] = anomalias.has(`${nome}|${m}`); });
          return { nome, porMes, anom };
        });
    },

    get anomaliasCatComp() {
      const ac = (this.comp && this.comp.anomalias_categoria) || [];
      return ac.map((a, i) => ({ ...a, chave: `${a.categoria}|${a.mes}|${i}` }));
    },
    get anomaliasTxComp() {
      const at = (this.comp && this.comp.anomalias_transacao) || [];
      return at.map((a, i) => ({ ...a, chave: `${a.descricao}|${a.data}|${a.valor}|${i}` }));
    },

    // Comparação com o 1º mês (base 100%): cor e texto da linha de variação
    _variacao(m) {
      const c = this.comp;
      if (!c) return { base: 0, pct: 0 };
      const base = parseFloat(c.totais_mes[c.meses[0]]);
      const v = parseFloat(c.totais_mes[m]);
      return { base, pct: base ? ((v - base) / base) * 100 : 0 };
    },
    variacaoTexto(m) {
      const { base, pct } = this._variacao(m);
      if (!base) return "—";
      return `${pct > 0 ? "+" : ""}${pct.toFixed(0)}%`;
    },
    variacaoCor(m) {
      const { base, pct } = this._variacao(m);
      if (!base) return "";   // sem base: célula sem cor inline (igual ao antes)
      return pct > 1 ? "cor-red" : pct < -1 ? "cor-green" : "cor-muted";
    },

    // ------------------------------------------------------- helpers de linha
    transacaoPorId(id) {
      return ((this.fatura && this.fatura.transacoes) || [])
        .find(x => String(x.id) === String(id));
    },

    // Rótulo CADASTRADO que casa com o texto digitado (comparação normalizada) —
    // preserva a grafia existente: "alimentacao" -> "Alimentação".
    categoriaConhecidaPor(texto) {
      const alvo = normalizarDescricao(texto);
      if (!alvo) return null;
      for (const c of this.categoriasConhecidas) {
        if (normalizarDescricao(c) === alvo) return c;
      }
      return null;
    },

    ehRecorrente(t) {
      return this.recorrentes.has(normalizarDescricao(t.descricao || ""));
    },

    editorAberto(t) {
      return !!this.editor && String(this.editor.id) === String(t.id);
    },

    // Flash "aprendido" (outline --accent ~800ms): o estado re-aplica a classe a
    // cada re-render e o timer a remove — antes isso era feito na mão pós-render.
    aprendido(t) {
      return !!this.aprendizadoFlash
        && normalizarDescricao(t.descricao) === this.aprendizadoFlash.chave;
    },
    marcarAprendizado(chave) {
      this.aprendizadoFlash = { chave };
      clearTimeout(this._tAprend);
      this._tAprend = setTimeout(() => { this.aprendizadoFlash = null; }, 850);
    },

    // Cabeçalho ordenável (só Data e Valor), com a seta do CSS
    classeTh(c) {
      const sortable = (c.key === "data" || c.key === "valor") ? "sortable" : "";
      const sorted = c.key === this.sort.key ? `sorted-${this.sort.dir}` : "";
      return `${sortable} ${sorted}`;
    },
    ordenarPor(key) {
      if (key !== "data" && key !== "valor") return;   // só essas colunas são ordenáveis
      if (this.sort.key === key) {
        this.sort.dir = this.sort.dir === 'asc' ? 'desc' : 'asc';
      } else {
        this.sort.key = key;
        this.sort.dir = 'asc';
      }
    },

    // ---------------------------------------------------------------- init
    async iniciar() {
      try {
        this.parsers = await apiGET("/api/parsers");
      } catch(e) {
        console.error("Erro carregando parsers:", e);
      }

      await this.carregarRecorrentes();            // flag global: precisa antes do 1º render
      await this.carregarCategoriasConhecidas();   // rótulos (regras + overrides) p/ autocomplete
      await this.carregarFaturas();

      this.iniciarScrollSpy();
      await this.route();                          // aplica a view da URL (F5 / link direto)
    },

    // ===== Carregar lista de faturas =====
    async carregarFaturas() {
      try {
        this.faturas = await apiGET("/api/faturas");
      } catch(e) {
        console.error("Erro carregando faturas:", e);
      }
    },

    // ===== Carregar fatura específica =====
    async carregarFatura(id) {
      try {
        this.fatura = await apiGET(`/api/faturas/${id}`);

        // Popular selects apenas uma vez por fatura
        this.selectsPopulados = false;

        // Reset sort ao trocar de fatura
        this.sort = { key: "data", dir: 'asc' };

        // Flag global de recorrentes: recarrega a cada fatura para não ficar stale
        await this.carregarRecorrentes();

        this.renderizarViewer();
        return true;                               // quem troca de tela é o route()
      } catch(e) {
        console.error("Erro carregando fatura:", e);
        return false;                              // route() trata o 404
      }
    },

    async deletarFatura(id) {
      if (!id) return;
      if (!confirm("Excluir esta fatura e o PDF associado?")) return;

      try {
        await apiDELETE(`/api/faturas/${id}`);
        await this.carregarFaturas();

        // Se a fatura atual foi deletada NÃO escolhemos outra sozinhos: volta ao
        // placeholder + tela de upload e o usuário seleciona no dropdown (FRONT 3).
        if (this.fatura && this.fatura.id === id) {
          this.fatura = null;
          this.redirecionar("/");                  // não deixa /view/<id> morto no histórico
        }
      } catch(err) {
        if (err.message.includes("404")) {
          alert("Fatura não encontrada");
        } else {
          console.error("Erro deletando:", err);
          alert(`Erro ao excluir: ${err.message}`);
        }
        await this.carregarFaturas();
      }
    },

    // ===== Navegar entre telas =====
    // A anchor-nav aparece só na tela-viewer (antes: classe .hidden, hoje x-show)
    mostrarTelas(tela) {
      this.tela = tela;
    },

    // ===== Rotas de URL (history API) =====
    // Mapa URL → view:
    //   /                → upload (tela inicial; lista/dropdown de faturas)
    //   /view/<uuid>     → viewer (detalhe da fatura)
    //   /dicionario      → dicionario
    //   /comparativo     → comparativo
    //   /consolidado     → consolidado
    // route() é o ÚNICO lugar que troca de tela: os botões chamam navigar() e
    // as funções de fetch existentes continuam sendo as mesmas (nada duplicado).
    async route() {
      const token = ++this.rotaToken;      // marca esta navegação como a atual
      const path = location.pathname.replace(/\/+$/, "") || "/";
      const m = path.match(/^\/view\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i);

      this.carregando = true;
      try {
        if (m) {
          this.view = { name: "viewer", id: m[1] };
          const ok = await this.carregarFatura(m[1]);
          if (token !== this.rotaToken) return;             // outra navegação venceu
          if (!ok) { this.redirecionar("/", "Fatura não encontrada."); return; }
          this.mostrarTelas("viewer");
          return;
        }
        if (path === "/dicionario") {
          this.view = { name: "dicionario", id: null };
          this.mostrarTelas("dicionario");
          this.carregarRegrasDic();
          this.carregarOverridesDic();
          return;
        }
        if (path === "/comparativo") {
          this.view = { name: "comparativo", id: null };
          this.mostrarTelas("comparativo");
          await this.carregarComparativo();
          return;
        }
        if (path === "/consolidado") {
          this.view = { name: "consolidado", id: null };
          this.mostrarTelas("consolidado");
          this.cons = null;               // sempre recarrega (upload/delete deixam stale)
          await this.carregarConsolidado();
          return;
        }
        if (path === "/") {
          this.view = { name: "upload", id: null };
          this.mostrarTelas("upload");
          return;
        }
        // URL desconhecida (ou /view/<id> fora do padrão UUID)
        this.redirecionar("/", "Página não encontrada.");
      } finally {
        if (token === this.rotaToken) this.carregando = false;
      }
    },

    // Clique em botão/atalho: empurra a URL e deixa route() aplicar o estado.
    // Caminho igual não cria entrada nova no histórico.
    navigar(path) {
      if (path !== location.pathname) history.pushState({}, "", path);
      this.route();
    },

    // Rota inválida / fatura inexistente: SUBSTITUI a entrada (com push o botão
    // Voltar voltaria pra URL ruim, em loop) e avisa na tela inicial.
    redirecionar(path, msg) {
      if (path !== location.pathname) history.replaceState({}, "", path);
      if (msg) this.uploadErro = msg;
      this.route();
    },

    // Botões "Voltar" das telas internas: mesma regra de antes (tem fatura
    // carregada? volta pro viewer; senão, pra tela inicial), agora via URL.
    voltar() {
      this.navigar(this.fatura ? `/view/${this.fatura.id}` : "/");
    },

    // ===== Upload =====
    novaFatura() {
      this.navigar("/");
      const form = document.getElementById("form-upload");
      if (form) form.reset();          // limpa o input de arquivo do DOM
      this.enviando = false;
      this.uploadErro = "";
      // form.reset() limpa o input de arquivo (e o select volta ao placeholder):
      // o estado do arquivo tem que sair junto. O RÓTULO com o nome do PDF anterior
      // permanece — é o mesmo comportamento de antes (o reset só mexe em controles).
      this.arquivo = null;
      this.banco = "";
      // A hint do arquivo anterior também precisa sair, senão fica um
      // "Banco detectado: X" órfão na tela limpa.
      this.bancoHint = { texto: "", tom: "" };
    },

    async aoEscolherArquivo(e) {
      const arquivo = e.target.files[0];
      this.arquivo = arquivo || null;
      this.arquivoNome = arquivo ? arquivo.name : PLACEHOLDER_ARQUIVO;

      this.banco = "";
      this.bancoHint = { texto: "", tom: "" };
      if (!arquivo) return;

      this.bancoHint = { texto: "Analisando fatura...", tom: "tom-muted" };
      const fd = new FormData();
      fd.append("arquivo", arquivo);
      try {
        const resp = await fetch("/api/inferir-banco", { method: "POST", body: fd });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const dados = await resp.json();
        if (dados.banco) {
          this.banco = dados.banco;
          const label = this.parsers[dados.banco] ? rotuloBanco(dados.banco) : dados.banco;
          this.bancoHint = { texto: `Banco detectado: ${label}. Se for outro, altere no menu.`, tom: "tom-green" };
        } else {
          this.bancoHint = { texto: "Não foi possível identificar o banco — selecione no menu.", tom: "tom-yellow" };
        }
      } catch (err) {
        this.bancoHint = { texto: "Não foi possível identificar o banco — selecione no menu.", tom: "tom-yellow" };
      }
    },

    async enviarFatura() {
      const arquivo = this.arquivo;
      const banco = this.banco;

      if (!arquivo || !banco) {
        this.uploadErro = "Selecione um PDF e um banco.";
        return;
      }

      // Loading
      this.enviando = true;
      this.uploadErro = "";

      const formData = new FormData();
      formData.append("arquivo", arquivo);
      formData.append("banco", banco);

      try {
        const resp = await fetch("/api/faturas", { method: "POST", body: formData });
        const text = await resp.text();

        // Só tenta JSON.parse se content-type for application/json
        const contentType = resp.headers.get("content-type") || "";
        const isJSON = contentType.includes("application/json");

        let data = null;
        if (isJSON) {
          try {
            data = JSON.parse(text);
          } catch(e) {
            // JSON mal formado
          }
        }

        if (resp.status === 422) {
          const erro = (data && data.erro) || `Erro do servidor (HTTP ${resp.status}): ${text.substring(0, 200)}`;
          this.uploadErro = erro;
        } else if (resp.status === 200 && data) {
          // Sucesso — carrega a fatura
          this.fatura = data;
          await this.carregarFaturas(); // atualiza lista
          this.renderizarViewer();
          this.navigar(`/view/${data.id}`);        // URL do detalhe da fatura enviada
        } else {
          // Nao-JSON ou status inesperado — debug remoto
          this.uploadErro = `Erro do servidor (HTTP ${resp.status}): ${text.substring(0, 200)}`;
        }
      } catch(err) {
        this.uploadErro = `Erro inesperado: ${err.message}`;
      } finally {
        this.enviando = false;
      }
    },

    // ===== Command palette: seleção de fatura com busca =====
    abrirCmdk() {
      // Sem faturas: o placeholder desabilitado nem abre o palette (só upload)
      if (!this.faturas.length) return;
      this.cmdkAberto = true;
      this.cmdkBusca = "";
      this.cmdkAtivo = -1;
      this.$nextTick(() => {
        const inp = document.getElementById("cmdk-input");
        if (inp) inp.focus();
      });
    },

    fecharCmdk() {
      this.cmdkAberto = false;
    },

    aoDigitarCmdk() {
      this.cmdkAtivo = -1;   // cada busca re-renderizava a lista com a seleção zerada
    },

    moverCmdk(passo) {
      const itens = this.cmdkVisiveis;
      if (!itens.length) return;
      this.cmdkAtivo = passo > 0
        ? Math.min(this.cmdkAtivo + 1, itens.length - 1)
        : Math.max(this.cmdkAtivo - 1, 0);
      this.$nextTick(() => {
        const els = document.querySelectorAll("#cmdk-list .cmdk-item");
        const alvo = els[this.cmdkAtivo];
        if (alvo && typeof alvo.scrollIntoView === "function") {
          alvo.scrollIntoView({ block: "nearest" });
        }
      });
    },

    confirmarCmdk() {
      const itens = this.cmdkVisiveis;
      if (!itens.length) return;
      const alvo = itens[this.cmdkAtivo >= 0 ? this.cmdkAtivo : 0];
      if (alvo) this.selecionarCmdk(alvo.id);
    },

    selecionarCmdk(id) { this.fecharCmdk(); this.navigar(`/view/${id}`); },

    async deletarFaturaCmdk(id) {
      await this.deletarFatura(id);   // recarrega faturas: a lista já deriva do estado
    },

    // Atalhos globais (antes: dois listeners no document)
    atalhoGlobal(e) {
      if (e.key === "Escape" && this.cmdkAberto) this.fecharCmdk();
      // atalho Ctrl/Cmd+K abre o palette
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k" && this.faturas.length) {
        e.preventDefault();
        this.abrirCmdk();
      }
    },

    // ===== Render viewer =====
    renderizarViewer() {
      if (!this.fatura) return;

      // Ponto único por onde a fatura passa a ser exibida (clique no dropdown e
      // upload) → alimenta o cache de rótulos com as categorias desta fatura.
      this.registrarCategorias((this.fatura.transacoes || []).map(t => t.categoria));

      // Popular selects uma vez por fatura
      if (!this.selectsPopulados) {
        this.popularSelects();
        this.selectsPopulados = true;
      }

      // Aplicar filtros iniciais
      this.aplicarFiltros();
    },

    // ===== Popular selects de filtro (apenas uma vez por fatura) =====
    popularSelects() {
      const todas = (this.fatura && this.fatura.transacoes) || [];

      // Categoria — FORA DE ESCOPO do autocomplete: o filtro continua com as
      // categorias da fatura carregada (o cache global não entra aqui). Uma categoria
      // nova só aparece no filtro depois de recarregar a fatura; o select inline das
      // linhas, esse sim, já a mostra no próximo render (categoriasDisponiveis).
      this.categoriasFiltro = [...new Set(todas.map(t => t.categoria || "—"))].sort();

      // Cartão
      this.cartoesFiltro = [...new Set(todas.map(t => t.cartao || ""))].filter(c => c).sort();

      // O innerHTML dos selects zerava o valor selecionado ("Todas"/"Todos")
      this.filtroCategoria = "";
      this.filtroCartao = "";
    },

    // ===== Aplicar filtros =====
    aplicarFiltros() {
      // FIX: nao repopula selects — so le os valores
      const busca = (this.busca || "").toLowerCase();
      const catSel = this.filtroCategoria;
      const cartSel = this.filtroCartao;

      const todas = (this.fatura && this.fatura.transacoes) || [];

      // Filtrar
      this.linhas = todas.filter(t => {
        const desc = (t.descricao || "").toLowerCase();
        const matchBusca = !busca || desc.includes(busca);
        const matchCat = !catSel || (t.categoria || "—") === catSel;
        const matchCart = !cartSel || t.cartao === cartSel;
        return matchBusca && matchCat && matchCart;
      });

      // FIX: nao reseta sortConfig aqui — preserva a ordenacao do usuario.
      // A vista das categorias, essa sim, voltava para "pizza" a cada render.
      this.vistaCat = "pizza";

      // Projeção de parcelas futuras (usa sempre todas as transacoes da fatura)
      this.projetarParcelas();

      // A paleta de cores é greedy e PERSISTIDA (localStorage): a ORDEM da primeira
      // atribuição é o que define a cor de cada categoria. Antes essa ordem vinha da
      // sequência do render (linhas da tabela → totais por categoria → meses da
      // projeção); aqui ela é fixada de uma vez, sem depender da ordem dos efeitos
      // reativos do Alpine.
      this.atribuirCores();
    },

    atribuirCores() {
      this.linhasOrdenadas.forEach(t => corCategoria(t.categoria));
      paresCategoria(this.porCategoria).forEach(([cat]) => corCategoria(cat));
      this.blocosProj.forEach(b => corCategoria(b.label));
    },

    // ===== Cache global de rótulos (categoriasConhecidas) =====
    //
    // Só um cache para autocomplete/dedup — NÃO é fonte de verdade (a verdade segue
    // sendo data/categorias.json + os overrides). Semeado em iniciar() com a UNIÃO
    // de: categorias das regras, categorias dos overrides e categorias das transações
    // das faturas já carregadas. Alimentado também quando uma fatura é carregada,
    // quando uma regra é salva no Dicionário e quando o editor livre cria categoria.
    registrarCategorias(lista) {
      (lista || []).forEach(c => {
        const rotulo = String(c === null || c === undefined ? "" : c).trim();
        if (rotulo && rotulo !== "—") this.categoriasConhecidas.add(rotulo);
      });
    },

    // Semeia o cache com regras + overrides. Falha não derruba o app: o Set também é
    // alimentado pelas faturas carregadas e pelo Dicionário.
    async carregarCategoriasConhecidas() {
      try {
        const dados = await apiGET("/api/categorias");
        this.registrarCategorias((dados.regras || []).map(r => r.categoria));
      } catch (e) {
        console.warn("Não foi possível carregar /categorias (rótulos):", e.message);
      }
      try {
        const overrides = await apiGET("/api/overrides");
        this.registrarCategorias(Object.values(overrides || {}));
      } catch (e) {
        console.warn("Não foi possível carregar /overrides (rótulos):", e.message);
      }
    },

    // ===== Ensino de categoria (override global por descrição normalizada) =====

    // Núcleo COMPARTILHADO do ensino (select inline e editor livre): PUT do override
    // com a descrição CRUA da transação (o backend normaliza) e aplicação da
    // categoria em TODAS as transações da fatura com a mesma chave normalizada —
    // o override é global por descrição. Lança em falha; quem chama faz o revert.
    async ensinarCategoriaGlobal(descricaoCrua, rotulo) {
      let chave = normalizarDescricao(descricaoCrua);
      const resp = await apiPUT(`/api/overrides/${encodeURIComponent(descricaoCrua)}`, { categoria: rotulo });
      if (resp && resp.chave) chave = resp.chave;   // backend é a fonte da verdade

      let afetadas = 0;
      ((this.fatura && this.fatura.transacoes) || []).forEach(x => {
        if (normalizarDescricao(x.descricao) === chave) {
          x.categoria = rotulo;
          afetadas++;
        }
      });
      return { chave, afetadas };
    },

    async aprenderCategoria(e, t) {
      const sel = e.target;
      const anterior = sel.dataset.anterior || "";
      const nova = sel.value;
      if (!nova || nova === anterior) return;

      // Sentinela "＋ Outra categoria…": NÃO envia nada — troca a célula por um input
      // livre (x-if). O valor __OUTRA__ jamais chega ao backend e, como o select é
      // recriado do estado ao fechar, não há valor para restaurar.
      if (nova === SENTINELA_OUTRA) { this.abrirEdicaoCategoriaLivre(t, sel.getAttribute("style") || ""); return; }

      this.emCursoCat.add(String(t.id));
      let res;
      try {
        res = await this.ensinarCategoriaGlobal(t.descricao, nova);
      } catch (e2) {
        // Falha no PUT: volta o select para a categoria anterior e reabilita
        this.emCursoCat.delete(String(t.id));
        sel.value = anterior;
        alert(`Não foi possível ensinar a categoria: ${e2.message}`);
        return;
      }

      this.emCursoCat.delete(String(t.id));
      this.registrarCategorias([nova]);
      this.marcarAprendizado(res.chave);
      this.aplicarFiltros();   // re-render: tabela + totais por categoria na hora
      console.log(`[categoria] "${nova}" ensinada para ${res.afetadas} transação(ões) — chave "${res.chave}"`);
    },

    // ===== Editor livre de categoria (sentinela "＋ Outra categoria…") =====
    //
    // O select dá lugar a <input list="lista-categorias"> NA MESMA CÉLULA (x-if).
    // O input aceita QUALQUER texto (o datalist é só autocomplete, nunca restrição).
    // No máximo UM editor aberto por vez.

    abrirEdicaoCategoriaLivre(t, styleAnterior) {
      this.fecharEditorCategoria();   // só um editor por vez
      this.editor = { id: String(t.id), valor: "" };
      // começa vazio e com foco (o texto anterior não é pré-preenchido)
      this.$nextTick(() => {
        const el = document.querySelector("input.categoria-livre");
        if (el) el.focus();
      });
    },

    // Fecha SEM PUT: o select volta reconstruído do estado (categoria anterior)
    fecharEditorCategoria() {
      if (!this.editor) return;
      this.editor = null;
    },

    async confirmarEdicaoCategoriaLivre() {
      if (!this.editor) return;
      const texto = (this.editor.valor || "").trim();
      if (!texto) return;   // Enter vazio: sem efeito

      const t = this.transacaoPorId(this.editor.id);
      if (!t) { this.fecharEditorCategoria(); return; }

      // Casa com rótulo já conhecido? Usa a GRAFIA CADASTRADA ("alimentacao" ->
      // "Alimentação"). Senão, o texto digitado é a categoria nova.
      const rotulo = this.categoriaConhecidaPor(texto) || texto;

      this.emCursoCat.add(String(t.id));
      let res;
      try {
        res = await this.ensinarCategoriaGlobal(t.descricao, rotulo);   // MESMO caminho do ensino normal
      } catch (e) {
        // Falha no PUT: mantém o input ABERTO com o texto digitado (mesmo alert do
        // fluxo do select). Nunca traceback ao usuário.
        this.emCursoCat.delete(String(t.id));
        this.$nextTick(() => {
          const el = document.querySelector("input.categoria-livre");
          if (el) el.focus();
        });
        alert(`Não foi possível ensinar a categoria: ${e.message}`);
        return;
      }

      // Categoria nova entra no cache: categoriasDisponiveis já a inclui no próximo
      // render (senão o select voltaria sem ela) e o datalist a mostra ao reabrir.
      this.emCursoCat.delete(String(t.id));
      this.registrarCategorias([rotulo]);
      this.marcarAprendizado(res.chave);
      this.fecharEditorCategoria();
      this.aplicarFiltros();   // re-render: todas as linhas com a descrição recebem o rótulo
      console.log(`[categoria] "${rotulo}" ensinada para ${res.afetadas} transação(ões) — chave "${res.chave}"`);
    },

    // ===== Gasto recorrente (flag global por descrição normalizada) =====
    //
    // Ortogonal a categoria e a parcela: marca itens que repetem todo mês sem
    // parcelamento (WELLHUB, Apple Bill, Spotify...). O toggle vive na célula de
    // Parcela porque é ali que a ideia se lê: "sem parcelas, mas repete".
    // Mesma UX do ensino de categoria: PUT/DELETE + flash de ~800ms + aplica em
    // todas as linhas com a mesma descrição normalizada da fatura atual.

    // Carrega a flag global. Falha não derruba o viewer: sem a lista, nenhuma
    // linha aparece marcada (e o console avisa).
    async carregarRecorrentes() {
      try {
        const dados = await apiGET("/api/recorrentes");
        this.recorrentes = new Set(Object.keys(dados || {}));
      } catch (e) {
        this.recorrentes = new Set();
        console.warn("Não foi possível carregar /recorrentes:", e.message);
      }
    },

    async alternarRecorrente(t) {
      if (this.emCursoRec.has(String(t.id))) return;
      if (!t) return;

      const marcado = this.ehRecorrente(t);
      const chaveLocal = normalizarDescricao(t.descricao || "");
      let chave = chaveLocal;

      this.emCursoRec.add(String(t.id));
      try {
        if (marcado) {
          await apiDELETE(`/api/recorrentes/${encodeURIComponent(t.descricao)}`);
        } else {
          // t.descricao CRUA — o backend normaliza (chave = descrição normalizada)
          const resp = await apiPUT(`/api/recorrentes/${encodeURIComponent(t.descricao)}`, {});
          if (resp && resp.chave) chave = resp.chave;   // backend é a fonte da verdade
        }
      } catch (e) {
        this.emCursoRec.delete(String(t.id));
        alert(`Não foi possível ${marcado ? "desmarcar" : "marcar"} como recorrente: ${e.message}`);
        return;
      }
      this.emCursoRec.delete(String(t.id));

      // Flag GLOBAL por descrição normalizada: vale para TODAS as linhas iguais
      if (marcado) this.recorrentes.delete(chave);
      else this.recorrentes.add(chave);

      this.marcarAprendizado(chave);   // mesmo flash "aprendido" (outline --accent ~800ms)
      this.aplicarFiltros();           // re-render: tabela (🔁 + toggle) e projeção

      const afetadas = ((this.fatura && this.fatura.transacoes) || [])
        .filter(x => normalizarDescricao(x.descricao || "") === chave).length;
      console.log(`[recorrente] ${marcado ? "desmarcado" : "marcado"} "${chave}" — ${afetadas} transação(ões)`);
    },

    // ===== Projeção de Parcelas Futuras =====
    projetarParcelas() {
      // O toggle Gráfico/Lista reiniciava a cada render do bloco
      this.vistaProj = "grafico";
      this.projMensagem = "";
      this.blocosProj = [];
      this.pontosProj = [];

      const f = this.fatura;
      if (!f || !f.transacoes) return;

      const fechamento = f.fechamento;
      if (!fechamento) {
        this.projMensagem = "Sem data de fechamento para projeção.";
        return;
      }

      const base = new Date(fechamento + "T12:00:00");
      const porMes = {}; // { "AAAA-MM": { mesLabel, total, linhas: [{...}] } }
      const CAP_MESES = 12;   // mesmo teto das parcelas

      const adicionar = (t, k, valor, infoParcela) => {
        const d = new Date(base.getTime());
        d.setMonth(d.getMonth() + k);

        const y = d.getFullYear();
        const m = String(d.getMonth() + 1).padStart(2, "0");
        const key = `${y}-${m}`;
        const label = `${m}/${y}`;

        if (!porMes[key]) {
          porMes[key] = { mesLabel: label, total: 0, linhas: [] };
        }

        porMes[key].total += valor;
        porMes[key].linhas.push({
          descricao: t.descricao || "",
          cartao: t.cartao || "",
          parcela: infoParcela,              // "N/M" nas parcelas; null = recorrente
          recorrente: infoParcela === null,
          valor: valor
        });
      };

      f.transacoes.forEach(t => {
        const val = parseFloat(t.valor);
        if (!(val > 0)) return;

        // RECORRENTE VENCE: item marcado como recorrente é projetado como mensal
        // indefinido (teto de 12 meses) e tem as parcelas restantes ignoradas —
        // assim a mesma compra nunca é contada duas vezes na projeção.
        if (this.ehRecorrente(t)) {
          for (let k = 1; k <= CAP_MESES; k++) adicionar(t, k, val, null);
          return;
        }

        const pa = parseInt(t.parcela_atual);
        const pt = parseInt(t.parcela_total);

        if (pa != null && pt != null && pa > 0 && pa < pt) {
          const cap = Math.min(pt - pa, CAP_MESES);
          for (let k = 1; k <= cap; k++) adicionar(t, k, val, `${pa + k}/${pt}`);
        }
      });

      const chaves = Object.keys(porMes).sort();

      if (!chaves.length) {
        this.projMensagem = "Sem parcelas nem gastos recorrentes pendentes para próximas faturas.";
        return;
      }

      const maxTotal = Math.max(...chaves.map(k => porMes[k].total)) || 1;
      this.blocosProj = chaves.map((k, idx) => {
        const bloco = porMes[k];
        const detalhesHtml = bloco.linhas
          .sort((a, b) => b.valor - a.valor)
          .map(l => `
        <div class="projecao-linha" style="display:none;font-size:0.8rem;color:var(--muted);padding-left:16px;border-bottom:1px solid var(--border);">
          <span>${l.descricao}</span>
          <span style="float:right">${l.cartao} · ${l.recorrente
            ? '<span class="badge-proj-recorrente">' + ICONE_RECORRENTE + 'Recorrente</span>'
            : 'parcela ' + l.parcela + ' · ' + formatarMoeda(l.valor)}</span>
        </div>`).join("");
        return {
          label: bloco.mesLabel,
          total: bloco.total,
          pct: Math.round((bloco.total / maxTotal) * 100),
          detalhesHtml,
          aberto: idx === 0,   // primeiro bloco já vem expandido (como antes)
        };
      });
      this.pontosProj = this.blocosProj.map(b => ({ rotulo: b.label, valor: b.total }));
    },

    // ===== Tooltip dos pontos do gráfico de projeção (hover + clique/toque) =====
    // Delegação: funciona mesmo depois de cada re-render do SVG (o gráfico é
    // reconstruído a cada mudança de estado, os listeners são do body).
    aoHoverPonto(e) {
      const ponto = e.target.closest && e.target.closest("circle.linha-ponto");
      if (ponto) this.mostrarTooltipLinha(ponto, e.clientX, e.clientY);
    },
    aoMoverMouse(e) {
      if (this.tooltip.visivel) this.posicionarTooltip(e.clientX, e.clientY);
    },
    aoSairPonto(e) {
      if (e.target.closest && e.target.closest("circle.linha-ponto")) {
        this.esconderTooltipLinha();
      }
    },
    // Toque/clique: toggle perto do ponto (mobile não tem hover)
    aoClicarPonto(e) {
      const ponto = e.target.closest && e.target.closest("circle.linha-ponto");
      if (!ponto) { this.esconderTooltipLinha(); return; }
      const r = ponto.getBoundingClientRect();
      if (this.tooltip.visivel) {
        this.esconderTooltipLinha();
      } else {
        this.mostrarTooltipLinha(ponto, r.left + r.width / 2, r.top);
      }
    },
    mostrarTooltipLinha(ponto, x, y) {
      this.tooltip.rotulo = ponto.dataset.rotulo;
      this.tooltip.valor = formatarMoeda(ponto.dataset.valor);
      this.tooltip.visivel = true;
      // Só dá para medir depois que o elemento está visível (mesma ordem de antes)
      this.$nextTick(() => this.posicionarTooltip(x, y));
    },
    // Reposiciona se perto da borda direita/inferior da janela
    posicionarTooltip(x, y) {
      const tt = document.getElementById("tooltip-linha");
      if (!tt) return;
      const w = tt.offsetWidth, h = tt.offsetHeight;
      this.tooltip.left = Math.min(x + 12, window.innerWidth - w - 8);
      this.tooltip.top = Math.min(y + 12, window.innerHeight - h - 8);
    },
    esconderTooltipLinha() {
      this.tooltip.visivel = false;
    },

    // ===== Dicionário de categorias =====

    // Estado LOCAL à tela — nada é persistido sem os botões explícitos.
    mostrarMsgDic(chave, texto, tipo) {
      const alvo = this[chave];
      if (!alvo) return;
      alvo.texto = texto;
      alvo.tipo = tipo || "";
      if (this._dicTimers[chave]) { clearTimeout(this._dicTimers[chave]); this._dicTimers[chave] = null; }
      if (tipo === "ok") {
        this._dicTimers[chave] = setTimeout(() => {
          if (this[chave].texto === texto) { this[chave].texto = ""; this[chave].tipo = ""; }
          this._dicTimers[chave] = null;
        }, 3000);
      }
    },

    // --- Regras ---

    async carregarRegrasDic() {
      try {
        const data = await apiGET("/api/categorias");
        this.regras = (data.regras || []).map(r => novaRegra(
          r.categoria || "",
          Array.isArray(r.padroes) ? [...r.padroes] : [],
        ));
      } catch (e) {
        this.mostrarMsgDic("msgRegras", `Não foi possível carregar as regras: ${e.message}`, "erro");
      }
    },

    // Rola até o último bloco de regra e foca a categoria. Usado por "+ Nova regra" e
    // por "↑ regra": o bloco novo nasce EDITÁVEL — nada é persistido antes de
    // "Salvar regras".
    focarUltimoBlocoRegra() {
      const blocos = document.querySelectorAll("#lista-regras .regra-bloco");
      const ultimo = blocos[blocos.length - 1];
      if (!ultimo) return;
      if (typeof ultimo.scrollIntoView === "function") {
        ultimo.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
      const inp = ultimo.querySelector(".regra-categoria");
      if (inp) inp.focus();
    },

    // Validação client-side: categoria não-vazia (trimada) e >= 1 padrão não-vazio.
    // Retorna as regras prontas para envio, ou null (com destaque vermelho) se inválido.
    validarRegrasTela() {
      this.regras.forEach(r => { r._invalido = false; });
      const regras = this.regras.map(r => ({
        categoria: (r.categoria || "").trim(),
        padroes: padroesDe(r._texto),
      }));

      if (!regras.length) {
        this.mostrarMsgDic("msgRegras", "Adicione pelo menos 1 regra.", "erro");
        return null;
      }

      let invalidas = 0;
      regras.forEach((r, i) => {
        if (!r.categoria || !r.padroes.length) { this.regras[i]._invalido = true; invalidas++; }
      });
      if (invalidas) {
        this.mostrarMsgDic("msgRegras",
          `${invalidas} regra(s) com problema (destaque vermelho): categoria não-vazia e ao menos 1 padrão. Nada foi enviado.`, "erro");
        return null;
      }
      return regras;
    },

    adicionarRegra() {
      this.regras.push(novaRegra("", []));
      this.$nextTick(() => this.focarUltimoBlocoRegra());
    },

    removerRegra(idx) {
      this.regras.splice(idx, 1);
    },

    async salvarRegrasDic() {
      const regras = this.validarRegrasTela();
      if (!regras) return;   // inválido -> NAO envia
      try {
        const resp = await apiPUT("/api/categorias", { regras });
        const salvas = resp && resp.regras ? resp.regras : regras;
        this.regras = salvas.map(r => novaRegra(r.categoria, [...r.padroes]));
        this.registrarCategorias(this.regras.map(r => r.categoria));   // regras salvas viram rótulos conhecidos
        this.mostrarMsgDic("msgRegras", `Regras salvas (${this.regras.length}).`, "ok");
      } catch (e) {
        this.mostrarMsgDic("msgRegras", e.message, "erro");   // 422 -> campo "erro" do JSON
      }
    },

    // --- Overrides ---

    async carregarOverridesDic() {
      try {
        this.overrides = await apiGET("/api/overrides");
        this.overridesErro = "";
      } catch (e) {
        this.overridesErro = e.message;
      }
    },

    async removerOverrideDic(chave) {
      try {
        await apiDELETE(`/api/overrides/${encodeURIComponent(chave)}`);   // chave já vem normalizada
        this.mostrarMsgDic("msgOverrides", `Ensinamento removido: ${chave}`, "ok");
      } catch (e) {
        this.mostrarMsgDic("msgOverrides", `Não foi possível remover: ${e.message}`, "erro");
      }
      await this.carregarOverridesDic();   // recarrega a lista
    },

    // --- "↑ regra": promove um ensinamento manual a REGRA ---------------------------
    // Item 5 do FRONT 3. Cria a regra com categoria = categoria do override e padrão =
    // chave normalizada, REMOVE o override e deixa o bloco EDITÁVEL na lista de regras
    // (a persistência só acontece em "Salvar regras").
    async converterOverrideEmRegra(chave) {
      if (!(chave in this.overrides)) return;
      if (!confirm(MSG_CONVERTER_REGRA)) return;

      const categoria = this.overrides[chave] || "";
      this.regras.push(novaRegra(categoria, [chave]));
      this.$nextTick(() => this.focarUltimoBlocoRegra());

      try {
        await apiDELETE(`/api/overrides/${encodeURIComponent(chave)}`);
        this.mostrarMsgDic("msgOverrides",
          `Ensino convertido em regra: "${chave}" → ${categoria || "(sem categoria)"}. Revise o bloco de regras e clique em “Salvar regras”.`, "ok");
      } catch (e) {
        this.mostrarMsgDic("msgOverrides",
          `Bloco de regra criado, mas não foi possível remover o ensino: ${e.message}`, "erro");
      }
      await this.carregarOverridesDic();   // recarrega a lista (o ensino já não deve estar lá)
    },

    // --- Aplicar global ---

    async aplicarEmTodasFaturas() {
      if (!confirm("Reaplicar regras + ensinamentos em todas as faturas salvas?")) return;
      this.recategorizando = true;
      try {
        const resp = await apiPOST("/api/recategorizar");
        this.mostrarMsgDic("msgRecat", `${resp.recategorizadas} faturas recategorizadas.`, "ok");
        if (this.fatura) {
          this.fatura = await apiGET(`/api/faturas/${this.fatura.id}`);
          this.selectsPopulados = false;   // categorias novas devem entrar no filtro
          this.renderizarViewer();
        }
      } catch (e) {
        this.mostrarMsgDic("msgRecat", `Falha ao recategorizar: ${e.message}`, "erro");
      } finally {
        this.recategorizando = false;
      }
    },

    abrirDicionario() {
      this.navigar("/dicionario");       // route() troca a tela e carrega os dados
    },

    // ===== Comparativo mês a mês =====
    abrirComparativo() {
      this.navigar("/comparativo");
    },

    async carregarComparativo() {
      this.compErro = "";
      this.compVazio = false;
      this.comp = null;

      let c;
      try {
        c = await apiGET("/api/comparativo");
      } catch (e) {
        this.compErro = `Não foi possível carregar o comparativo: ${e.message}`;
        return;
      }

      if (!c.meses || c.meses.length < 2) {
        this.compVazio = true;
        return;
      }
      this.comp = c;
    },

    // ===== Consolidado: todas as faturas somadas por mês + projeção =====
    abrirConsolidado() {
      this.navigar("/consolidado");
    },

    async carregarConsolidado() {
      this.consErro = "";
      this.consVazio = false;
      this.cons = null;

      let c;
      try {
        c = await apiGET("/api/consolidado");
      } catch (e) {
        this.consErro = `Não foi possível carregar: ${e.message}`;
        return;
      }
      if (!c.meses || c.meses.length === 0) {
        this.consVazio = true;
        return;
      }

      this.cons = c;
      // Mês mais recente como padrão
      this.consMes = c.meses[c.meses.length - 1];
      this.vistaConsCat = "pizza";
      this.renderConsProj(c);
      this.atribuirCoresConsolidado();
    },

    // Mesma garantia de ordem de atribuição de cores do viewer (ver atribuirCores)
    atribuirCoresConsolidado() {
      paresCategoria(this.consPorCategoria).forEach(([cat]) => corCategoria(cat));
      this.blocosConsProj.forEach(b => corCategoria(b.label));
    },

    renderConsProj(c) {
      // O toggle Gráfico/Lista reiniciava a cada render do bloco
      this.vistaConsProj = "grafico";
      this.consProjMensagem = "";
      this.blocosConsProj = [];
      this.pontosConsProj = [];

      const chaves = Object.keys(c.projecao || {});
      if (!chaves.length) {
        this.consProjMensagem = "Sem parcelas nem recorrentes pendentes.";
        return;
      }
      const maxTotal = Math.max(...chaves.map(k => parseFloat(c.projecao[k].total))) || 1;
      this.blocosConsProj = chaves.map((k, idx) => {
        const bloco = c.projecao[k];
        const label = rotuloMes(k);
        const total = parseFloat(bloco.total);
        const detalhesHtml = bloco.linhas.map(l =>
          `<div style="display:none;font-size:0.8rem;color:var(--muted);padding-left:16px;border-bottom:1px solid var(--border);">
            <span>${escHtml(l.descricao)}</span>
            <span style="float:right">${l.cartao ? escHtml(l.cartao) + " · " : ""}${l.info === "Recorrente"
              ? '<span class="badge-proj-recorrente">' + ICONE_RECORRENTE + 'Recorrente</span> · ' + formatarMoeda(l.valor)
              : "parcela " + escHtml(l.info) + " · " + formatarMoeda(l.valor)}</span>
          </div>`).join("");
        return {
          label,
          total,
          pct: Math.round((total / maxTotal) * 100),
          detalhesHtml,
          aberto: idx === 0,
        };
      });
      this.pontosConsProj = this.blocosConsProj.map(b => ({ rotulo: b.label, valor: b.total }));
    },

    // ===== Anchor Navigation (scroll-spy + smooth scroll) =====
    iniciarScrollSpy() {
      const nav = document.getElementById("anchor-nav");
      if (!nav) return;

      const sections = ["sec-resumo", "sec-transacoes", "sec-categorias", "sec-projecao"];

      // --- IntersectionObserver para scroll-spy ---
      const observer = new IntersectionObserver((entries) => {
        // Ignorar eventos durante o scroll programático (~600ms após clique)
        if (this.scrollIntencional) return;
        entries.forEach(entry => {
          if (entry.isIntersecting) {
            this.secaoAtiva = entry.target.id;
          }
        });
      }, {
        root: null,
        threshold: 0,
        rootMargin: "0px 0px -70% 0px"
      });

      // Observar as sections
      sections.forEach(id => {
        const sec = document.getElementById(id);
        if (sec) observer.observe(sec);
      });
    },

    // Clique/teclado em cada bolinha da anchor-nav
    irPara(target) {
      if (target === "topo") {
        window.scrollTo({ top: 0, behavior: "smooth" });
        this.secaoAtiva = "topo";   // o código anterior também marcava a bolinha clicada
      } else {
        const sec = document.getElementById(target);
        if (sec) sec.scrollIntoView({ behavior: "smooth" });
        this.secaoAtiva = target;
      }
      // Marcar a bolinha clicada como ativa e bloquear scroll-spy por ~600ms
      this.scrollIntencional = true;
      setTimeout(() => { this.scrollIntencional = false; }, 600);
    },
  }));
});
