declare module 'wkt' {
  export function parse(wktString: string): any;
}

declare module 'togeojson' {
  export function kml(xmlDoc: any): any;
  export function gpx(xmlDoc: any): any;
}

declare module 'shpjs' {
  export function parseZip(arrayBuffer: ArrayBuffer): Promise<any>;
}
