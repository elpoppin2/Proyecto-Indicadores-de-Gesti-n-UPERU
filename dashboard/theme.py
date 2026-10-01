"""Color único para los gráficos del dashboard. Definido por el usuario: fondo
blanco, menú negro, gráficos en rojo. Todos los gráficos de este proyecto son de
una sola serie (magnitud por categoría/mes), así que un solo rojo validado basta —
no hace falta una paleta categórica con separación CVD entre tonos.

ROJO_CHART es el slot 8 ("red") de la paleta de referencia validada (contraste y
separación CVD comprobados contra una superficie casi blanca, #fcfcfb), que es el
mismo tono usado como primaryColor en .streamlit/config.toml."""

ROJO_CHART = "#e34948"


def estilo_barras(fig):
    """Aplica el rojo del tema a un gráfico de barras (px.bar) de una sola serie."""
    fig.update_traces(marker_color=ROJO_CHART)
    fig.update_xaxes(showgrid=False)
    fig.update_yaxes(showgrid=False)
    return fig


def estilo_lineas(fig):
    """Aplica el rojo del tema a un gráfico de líneas (px.line) de una sola serie."""
    fig.update_traces(line_color=ROJO_CHART, marker_color=ROJO_CHART, line_width=2, marker_size=8)
    fig.update_xaxes(showgrid=False)
    fig.update_yaxes(showgrid=False)
    return fig
